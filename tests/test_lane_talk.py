"""Boundary cases of `fa lanes talk` (D55 addendum) with fake terminals and no real processes."""

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import subprocess

import pytest

from foragent import lane_talk, lanes
from foragent.config import Config
from foragent.state import StateStore

NOW = datetime(2026, 10, 27, 12, tzinfo=UTC)


class Terminals:
    def __init__(self, monkeypatch, *, opener="orca", queue_fails=False, alive=True):
        self.calls, self.alive, self.on_call = [], alive, None
        for key in ("ORCA_TERMINAL_HANDLE", "TMUX"):
            monkeypatch.delenv(key, raising=False)
        if opener == "orca":
            monkeypatch.setenv("ORCA_TERMINAL_HANDLE", "source")
        if opener == "tmux":
            monkeypatch.setenv("TMUX", "socket")
        available = {"codex", "claude", opener}
        monkeypatch.setattr(lane_talk.shutil, "which", lambda name: f"/bin/{name}" if name in available else None)
        monkeypatch.setattr("foragent.orch_spawn.shutil.which",
                            lambda name: f"/bin/{name}" if name in available else None)

        def run(argv, **kwargs):
            self.calls.append(list(argv))
            if self.on_call is not None:
                self.on_call(list(argv))
            name = Path(argv[0]).name
            if name == "codex" and queue_fails:
                raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))
            if not self.alive and argv[1:3] in (["terminal", "show"], ["select-window", "-t"], ["list-windows", "-a"]):
                raise subprocess.CalledProcessError(1, argv)
            stdout = {"orca": json.dumps({"handle": "term_1"}), "tmux": "@7\n"}.get(name, "")
            return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

        monkeypatch.setattr(lane_talk.subprocess, "run", run)

    def opened(self):
        return [argv for argv in self.calls if argv[1:3] == ["terminal", "create"] or argv[1:2] == ["new-window"]]


@pytest.fixture
def store(tmp_path):
    store = StateStore(tmp_path / "state", now=lambda: NOW)
    store.start_run(1, branch="foragent/issue-1", worktree=store.worktree_path(1), pid=None)
    return store


def session(store, executor, status="running"):
    if status != "running":
        store.finish_run(1, "blocked")
    path = store.root / "runs/1.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record.update(session_id="thread-1", session_executor=executor)
    path.write_text(json.dumps(record), encoding="utf-8")


def config(**values):
    return Config(**{"executor": "codex", "repo": "owner/repo", "language": "en", **values})


def test_failed_queue_exits_nonzero_keeps_log_open_and_records_nothing(store, tmp_path, monkeypatch):
    session(store, "codex")
    terminals = Terminals(monkeypatch, queue_fails=True)
    with pytest.raises(lane_talk.TalkError):
        lanes.talk(1, message="hi", store=store, config=config(), repo_root=tmp_path)
    assert len(terminals.opened()) == 1
    assert ["--message", "hi"] == terminals.calls[-1][-2:]
    assert store.get_run(1).talking is False
    assert not lane_talk.talk_path(store, 1).exists()


def test_claude_message_appends_to_unused_hint_with_one_blank_line(store, tmp_path, monkeypatch):
    session(store, "claude")
    Terminals(monkeypatch)
    hint = store.root / "hints/1.txt"
    hint.parent.mkdir(parents=True)
    hint.write_text("Earlier hint.\n", encoding="utf-8")
    lanes.talk(1, message="Second.", store=store, config=config(executor="claude"), repo_root=tmp_path)
    assert hint.read_text(encoding="utf-8") == "Earlier hint.\n\nSecond."


def test_running_claude_without_session_still_opens_log_and_saves_hint(store, tmp_path, monkeypatch):
    terminals = Terminals(monkeypatch)
    line = lanes.talk(1, message="Note.", store=store, config=config(executor="claude"), repo_root=tmp_path)
    assert "next attempt" in line
    assert (store.root / "hints/1.txt").read_text(encoding="utf-8") == "Note."
    assert len(terminals.opened()) == 1 and store.get_run(1).talking is True


def test_no_opener_prints_command_and_still_sends_message(store, tmp_path, monkeypatch):
    session(store, "codex")
    terminals = Terminals(monkeypatch, opener="none")
    line = lanes.talk(1, message="hi", store=store, config=config(), repo_root=tmp_path)
    assert "tail -f -F" in line and str(store.log_path(1)) in line
    assert [Path(argv[0]).name for argv in terminals.calls] == ["codex"]
    assert store.get_run(1).talking is False


def test_tmux_records_window_id_and_reshows_instead_of_opening_again(store, tmp_path, monkeypatch):
    session(store, "codex")
    terminals = Terminals(monkeypatch, opener="tmux")
    lanes.talk(1, store=store, config=config(), repo_root=tmp_path)
    assert json.loads(lane_talk.talk_path(store, 1).read_text())["handle"] == "@7"
    line = lanes.talk(1, store=store, config=config(), repo_root=tmp_path)
    assert "Showed" in line and len(terminals.opened()) == 1
    assert ["/bin/tmux", "select-window", "-t", "@7"] in terminals.calls


def test_terminal_app_has_no_handle_so_talking_stays_unset(store, tmp_path, monkeypatch):
    session(store, "codex")
    Terminals(monkeypatch, opener="osascript")
    line = lanes.talk(1, store=store, config=config(), repo_root=tmp_path)
    assert "stall checks keep running" in line
    assert store.get_run(1).talking is False and not lane_talk.talk_path(store, 1).exists()


def test_blocked_message_becomes_first_resume_prompt(store, tmp_path, monkeypatch):
    session(store, "codex", status="blocked")
    terminals = Terminals(monkeypatch)
    lanes.talk(1, message="Why blocked?", store=store, config=config(), repo_root=tmp_path)
    command = terminals.opened()[0]
    assert "resume thread-1 'Why blocked?'" in command[command.index("--command") + 1]
    assert not any(Path(argv[0]).name == "codex" for argv in terminals.calls)


def test_closed_terminal_adds_paused_time_and_restarts_stall_clock(store, tmp_path, monkeypatch):
    session(store, "codex")
    terminals = Terminals(monkeypatch)
    lanes.talk(1, store=store, config=config(), repo_root=tmp_path)
    run = store.get_run(1)
    assert run.talking and run.talking_since == NOW.isoformat()
    run, still_open = lane_talk.check_talking(store, run, tmp_path, NOW + timedelta(minutes=5))
    assert still_open
    terminals.alive = False
    later = NOW + timedelta(minutes=9)
    run, still_open = lane_talk.check_talking(store, run, tmp_path, later)
    assert not still_open and run.talking is False and run.waited_seconds == 9 * 60
    assert run.last_progress_at == later.isoformat() and not lane_talk.talk_path(store, 1).exists()


def test_finishing_a_talking_run_clears_talk_state(store, tmp_path, monkeypatch):
    session(store, "codex")
    Terminals(monkeypatch)
    lanes.talk(1, store=store, config=config(), repo_root=tmp_path)
    finished = store.finish_run(1, "merged")
    assert finished.talking is False and not lane_talk.talk_path(store, 1).exists()


def test_running_talk_ignores_a_session_recorded_for_another_executor(store, tmp_path, monkeypatch):
    session(store, "claude")  # An earlier run's Claude session; the lane now runs Codex.
    terminals = Terminals(monkeypatch)
    with pytest.raises(lane_talk.TalkError):
        lanes.talk(1, message="hi", store=store, config=config(), repo_root=tmp_path)
    assert terminals.calls == []


def test_runner_records_a_live_session_before_the_executor_returns(store, tmp_path):
    from types import SimpleNamespace

    from foragent.executors import ExecResult
    from foragent.run_execution import _run_executor

    log = store.log_path(1)
    log.parent.mkdir(parents=True, exist_ok=True)
    seen = []

    class Executor:
        def run(self, **kwargs):
            with log.open("a") as stream:
                stream.write(json.dumps({"type": "thread.started", "thread_id": "live-thread"}) + "\n")
            poll()  # The gate-facing poll runs while the worker is still going.
            seen.append(store.get_run(1).session_id)
            return ExecResult(0, False, "", log)

    def hook(execute, polling):
        nonlocal poll
        poll = polling
        return execute()

    poll = None
    ctx = SimpleNamespace(store=store, now=lambda: NOW, poll_hook=hook, config=config())
    _run_executor(1, ctx, Executor(), log_path=log,
                  on_session=lambda identity: store.update_run(1, session_id=identity, session_executor="codex"))
    assert seen == ["live-thread"]


def test_runtime_budget_excludes_an_open_talk_and_extends_the_executor_deadline(store):
    from types import SimpleNamespace

    from foragent import processes
    from foragent.run_waits import _remaining

    assert store.get_run(1).started_at == NOW.isoformat()
    store.update_run(1, talking=True, talking_since=(NOW + timedelta(minutes=10)).isoformat())
    ctx = SimpleNamespace(store=store, now=lambda: NOW + timedelta(minutes=50),
                          config=config(max_runtime_minutes=30))
    assert _remaining(1, ctx) == 20 * 60  # 50 minutes passed; 40 of them were the open talk.

    class Process:
        args = ["worker"]

        def __init__(self):
            self.waits = []

        def wait(self, timeout=None):
            self.waits.append(timeout)
            if len(self.waits) < 3:
                raise subprocess.TimeoutExpired(self.args, timeout)

    paused = iter([5.0, 35.0, 55.0])  # Cumulative paused seconds: at launch, then at each expiry.
    process = Process()
    with processes.extend_deadline(lambda: next(paused)):
        processes._wait_with_deadline(process, 10)
    assert process.waits == [10, 30.0, 20.0]  # Each extension is the pause added meanwhile.
    with processes.extend_deadline(lambda: 5.0), pytest.raises(subprocess.TimeoutExpired):
        processes._wait_with_deadline(Process(), 10)  # No pause during the command: original limit.
    with pytest.raises(subprocess.TimeoutExpired):
        processes._wait_with_deadline(Process(), 10)  # Outside a run nothing extends.


def test_talk_credit_and_runner_wait_credit_both_count(store):
    store.update_run(1, talking=True, talking_since=NOW.isoformat())
    store.add_waited(1, 30)  # A runner wait credited while the talk is open.
    ended = store.end_talk(1, NOW + timedelta(minutes=2))
    store.add_waited(1, 15)
    assert ended.waited_seconds == 30 + 120 and store.get_run(1).waited_seconds == 165


def test_hint_appended_during_an_attempt_survives_its_consumption(tmp_path):
    from foragent.run_previous import consume_hint, hint_path

    path = hint_path(tmp_path, 1)
    path.parent.mkdir(parents=True)
    path.write_text("Delivered.\n\nAdded while running.", encoding="utf-8")
    consume_hint(tmp_path, 1, "Delivered.\n")
    assert path.read_text(encoding="utf-8") == "Added while running."
    consume_hint(tmp_path, 1, None)  # No hint was delivered: the waiting one stays.
    assert path.read_text(encoding="utf-8") == "Added while running."
    consume_hint(tmp_path, 1, "Added while running.")
    assert not path.exists()


def test_terminal_app_receives_non_ascii_text_as_a_plain_argument(store, tmp_path, monkeypatch):
    session(store, "codex", status="blocked")
    terminals = Terminals(monkeypatch, opener="osascript")
    root = tmp_path / "저장소"
    root.mkdir()
    lanes.talk(1, message="한국어 메시지", store=store, config=config(), repo_root=root)
    argv = terminals.calls[0]
    assert "\\u" not in " ".join(argv)
    assert "한국어 메시지" in argv[-1] and str(root) in argv[-1]
    assert "do script (item 1 of argv)" in " ".join(argv)


def test_tick_leaves_a_talk_alone_while_another_talk_command_holds_the_issue(store, tmp_path, monkeypatch):
    session(store, "codex")
    terminals = Terminals(monkeypatch)
    lanes.talk(1, store=store, config=config(), repo_root=tmp_path)
    terminals.alive = False
    with lane_talk._issue_lock(store, 1):
        run, still_open = lane_talk.check_talking(store, store.get_run(1), tmp_path, NOW)
    assert still_open and store.get_run(1).talking is True
    run, still_open = lane_talk.check_talking(store, store.get_run(1), tmp_path, NOW)
    assert not still_open and store.get_run(1).talking is False


@pytest.mark.parametrize("has_worktree", [True, False])
def test_orca_terminal_starts_in_the_issue_worktree_or_repo_root(store, tmp_path, monkeypatch, has_worktree):
    session(store, "claude", status="blocked")
    terminals = Terminals(monkeypatch)
    if has_worktree:
        store.worktree_path(1).mkdir(parents=True)
    lanes.talk(1, store=store, config=config(executor="claude"), repo_root=tmp_path)
    create = terminals.opened()[0]
    expected = store.worktree_path(1) if has_worktree else tmp_path.resolve()
    assert create[create.index("--worktree") + 1] == f"path:{expected}"
    assert create[create.index("--command") + 1].split()[1:3] == ["--resume", "thread-1"]


@pytest.mark.parametrize("alive", [True, False])
def test_failed_queue_on_an_existing_talk_ends_it_and_drops_its_record(store, tmp_path, monkeypatch, alive):
    session(store, "codex")
    Terminals(monkeypatch)
    lanes.talk(1, store=store, config=config(), repo_root=tmp_path)
    assert store.get_run(1).talking and lane_talk.talk_path(store, 1).exists()
    # Reused (alive) or replaced (closed) terminal, then the send fails.
    terminals = Terminals(monkeypatch, queue_fails=True, alive=alive)
    with pytest.raises(lane_talk.TalkError):
        lanes.talk(1, message="hi", store=store, config=config(), repo_root=tmp_path)
    assert store.get_run(1).talking is False and not lane_talk.talk_path(store, 1).exists()
    assert len(terminals.opened()) == (0 if alive else 1)


def test_run_finishing_during_the_send_gets_no_running_terminal_record(store, tmp_path, monkeypatch):
    session(store, "codex")
    terminals = Terminals(monkeypatch)
    terminals.on_call = lambda argv: Path(argv[0]).name == "codex" and store.finish_run(1, "merged")
    lanes.talk(1, message="hi", store=store, config=config(), repo_root=tmp_path)
    assert store.get_run(1).status == "finished" and not lane_talk.talk_path(store, 1).exists()


def test_a_new_run_drops_an_earlier_runs_talk_record(store, tmp_path, monkeypatch):
    session(store, "codex", status="blocked")
    Terminals(monkeypatch)
    lanes.talk(1, store=store, config=config(), repo_root=tmp_path)
    assert lane_talk.talk_path(store, 1).exists()  # The resume terminal's record.
    store.start_run(1, branch="foragent/issue-1", worktree=store.worktree_path(1))
    assert not lane_talk.talk_path(store, 1).exists() and store.get_run(1).talking is False


def test_run_is_talking_while_its_terminal_opens_so_a_tick_cannot_stop_it(store, tmp_path, monkeypatch):
    session(store, "codex")
    terminals = Terminals(monkeypatch)
    seen = []

    def during(argv):
        if argv[1:3] == ["terminal", "create"]:
            run = store.get_run(1)
            seen.append((run.talking, lane_talk.check_talking(store, run, tmp_path, NOW)[1]))

    terminals.on_call = during
    lanes.talk(1, store=store, config=config(), repo_root=tmp_path)
    assert seen == [(True, True)] and store.get_run(1).talking is True


def test_no_handle_ends_the_provisional_talk(store, tmp_path, monkeypatch):
    session(store, "codex")
    Terminals(monkeypatch, opener="osascript")
    lanes.talk(1, store=store, config=config(), repo_root=tmp_path)
    run = store.get_run(1)
    assert run.talking is False and run.last_progress_at == run.started_at  # Stall clock untouched.


def test_runner_extends_every_command_timeout_by_the_runs_pause(store, monkeypatch):
    from types import SimpleNamespace

    from foragent import processes, runner

    seen = []
    monkeypatch.setattr(runner, "_execute", lambda n, ctx, worktree: seen.append(
        processes._paused_seconds.get()()) or "merged")
    monkeypatch.setattr(runner.board, "sync_status", lambda *a, **kw: None)
    monkeypatch.setattr(runner, "kick_tick", lambda root: None)
    store.update_run(1, waited_seconds=12, talking=True, talking_since=(NOW - timedelta(seconds=30)).isoformat())
    from foragent.tracker import FakeTracker

    ctx = runner.RunContext("owner/repo", Path("."), FakeTracker(now=lambda: NOW), store, config(),
                            SimpleNamespace(remove_worktree=lambda path: None), now=lambda: NOW)
    monkeypatch.setattr("foragent.run_implementation.lane_config", lambda config, lane: config)
    assert runner.run_issue(1, ctx) == "merged"
    assert seen == [42.0]
