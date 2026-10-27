"""Boundary cases of `fa lanes talk` (D55 addendum) with fake terminals and no real processes."""

from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import subprocess

import pytest

from foragent import lane_talk, lane_talk_open, lanes
from foragent.config import Config
from foragent.state import StateStore
from runner_support import run_case  # noqa: F401 - the shared runner fixture

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
        monkeypatch.setattr(lane_talk_open.shutil, "which", lambda name: f"/bin/{name}" if name in available else None)
        monkeypatch.setattr("foragent.orch_spawn.shutil.which",
                            lambda name: f"/bin/{name}" if name in available else None)

        def run(argv, **kwargs):
            self.calls.append(list(argv))
            if self.on_call is not None:
                self.on_call(list(argv))
            name = Path(argv[0]).name
            if name == "codex" and queue_fails:
                raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))
            probe = argv[1:3] == ["terminal", "show"] or "list-windows" in argv
            if probe and self.alive == "timeout":
                raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))
            if probe and not self.alive:
                # Real Orca answers a closed terminal's handle with an ok:false envelope.
                stale = {"id": "req-9", "ok": False, "error": {"code": "terminal_handle_stale"}}
                if name == "orca":
                    raise subprocess.CalledProcessError(1, argv, output=json.dumps(stale))
                # tmux's evidence: a working list-windows reply that leaves the window out.
                return subprocess.CompletedProcess(argv, 0, stdout="@1\n", stderr="")
            # Real Orca wraps answers in {"id": <request id>, "ok": true, "result": {...}}.
            envelope = {"id": "req-1", "ok": True, "result": {"terminal": {"handle": "term_1"}}}
            stdout = {"orca": json.dumps(envelope), "tmux": "@7\n"}.get(name, "")
            return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

        monkeypatch.setattr(lane_talk.subprocess, "run", run)

    def opened(self):
        return [argv for argv in self.calls if argv[1:3] == ["terminal", "create"] or "new-window" in argv]


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
    with pytest.raises(lane_talk_open.TalkError):
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
    assert ["/bin/tmux", "-S", "socket", "select-window", "-t", "@7"] in terminals.calls


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
    with pytest.raises(lane_talk_open.TalkError):
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
    with store.talk_lock(1):
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
    with pytest.raises(lane_talk_open.TalkError):
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


def test_a_talk_opening_between_the_ticks_read_and_its_stop_prevents_the_stop(store, monkeypatch):
    from types import SimpleNamespace

    from foragent import scheduler_recovery

    stops = []
    monkeypatch.setattr(scheduler_recovery, "_stop_run_processes",
                        lambda ctx, run, report, **kw: stops.append(run.number) or "refused")
    ctx, report = SimpleNamespace(store=store), SimpleNamespace(actions=[])
    run = store.get_run(1)  # The tick's read: not talking.
    assert run.talking is False
    store.begin_talk(1, run.started_at, NOW)  # fa lanes talk opens before the stop acts.
    assert scheduler_recovery._kill_run(ctx, 1, report, count_stall=True, unless_talking=True) is False
    assert stops == [] and store.get_run(1).restarts == 0
    store.end_talk(1, NOW)
    with store.talk_lock(1):  # A talk command is opening right now.
        assert scheduler_recovery._kill_run(ctx, 1, report, unless_talking=True) is False
    assert stops == []
    scheduler_recovery._kill_run(ctx, 1, report, unless_talking=True)  # No talk: the stop goes ahead.
    assert stops == [1]


def test_panel_reviewer_threads_get_the_runs_pause(store):
    from concurrent.futures import ThreadPoolExecutor
    import contextvars
    import threading

    from foragent import processes

    class Process:
        args = ["reviewer"]

        def __init__(self):
            self.waits = []

        def wait(self, timeout=None):
            self.waits.append(timeout)
            if len(self.waits) == 1:
                raise subprocess.TimeoutExpired(self.args, timeout)

    paused = iter([0.0, 90.0])

    def review():
        process = Process()
        processes._wait_with_deadline(process, 10)  # Would time out without the pause.
        return process.waits

    with processes.extend_deadline(lambda: next(paused)):
        with ThreadPoolExecutor(max_workers=1) as pool:
            copied = pool.submit(contextvars.copy_context().run, review).result()
        plain = []
        thread = threading.Thread(target=lambda: plain.append(processes.current_pause()))
        thread.start()
        thread.join()
    assert copied == [10, 90.0] and plain[0] is not None
    assert processes.current_pause() is None  # The run's pause ends with the run.


def test_tmux_talk_targets_its_own_server_socket_from_the_tick(store, tmp_path, monkeypatch):
    session(store, "codex")
    terminals = Terminals(monkeypatch, opener="tmux")
    monkeypatch.setenv("TMUX", "/tmp/custom-tmux-socket,4242,0")
    lanes.talk(1, store=store, config=config(), repo_root=tmp_path)
    create = terminals.opened()[0]
    assert create[1:4] == ["-S", "/tmp/custom-tmux-socket", "new-window"]
    assert json.loads(lane_talk.talk_path(store, 1).read_text())["socket"] == "/tmp/custom-tmux-socket"
    monkeypatch.delenv("TMUX")  # The gate tick runs outside tmux.
    run, still_open = lane_talk.check_talking(store, store.get_run(1), tmp_path, NOW)
    assert still_open
    assert terminals.calls[-1][1:4] == ["-S", "/tmp/custom-tmux-socket", "list-windows"]
    lanes.talk(1, store=store, config=config(), repo_root=tmp_path)  # Re-show selects on that server.
    assert terminals.calls[-1][1:4] == ["-S", "/tmp/custom-tmux-socket", "select-window"]


def test_finishing_after_a_ten_minute_talk_credits_600_waited_seconds(tmp_path):
    clock = [NOW]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    run = store.start_run(1, branch="foragent/issue-1", worktree=store.worktree_path(1))
    store.begin_talk(1, run.started_at, NOW)
    clock[0] = NOW + timedelta(minutes=10)
    finished = store.finish_run(1, "merged")
    assert finished.waited_seconds == 600 and finished.talking is False and finished.talking_since is None


def test_orca_handle_comes_from_the_result_terminal_not_the_request_id(store, tmp_path, monkeypatch):
    session(store, "codex")
    terminals = Terminals(monkeypatch)
    lanes.talk(1, store=store, config=config(), repo_root=tmp_path)
    assert json.loads(lane_talk.talk_path(store, 1).read_text())["handle"] == "term_1"
    lanes.talk(1, store=store, config=config(), repo_root=tmp_path)  # The same terminal is re-shown.
    assert len(terminals.opened()) == 1
    assert ["/bin/orca", "terminal", "show", "--terminal", "term_1", "--json"] in terminals.calls
    assert lane_talk._orca_handle(json.dumps({"id": "req-1", "ok": False, "error": {"code": "x"}})) is None


@pytest.mark.parametrize("opener", ["orca", "tmux"])
def test_a_probe_that_cannot_answer_keeps_the_talk(store, tmp_path, monkeypatch, opener):
    session(store, "codex")
    terminals = Terminals(monkeypatch, opener=opener)
    lanes.talk(1, store=store, config=config(), repo_root=tmp_path)
    terminals.alive = "timeout"
    run, still_open = lane_talk.check_talking(store, store.get_run(1), tmp_path, NOW)
    assert still_open and store.get_run(1).talking is True and lane_talk.talk_path(store, 1).exists()
    lanes.talk(1, store=store, config=config(), repo_root=tmp_path)  # No duplicate beside a maybe-open one.
    assert len(terminals.opened()) == 1
    terminals.alive = False  # Confirmed closed: Orca's stale-handle code, or no tmux server on the socket.
    run, still_open = lane_talk.check_talking(store, store.get_run(1), tmp_path, NOW)
    assert not still_open and store.get_run(1).talking is False


def test_orca_error_other_than_a_stale_handle_is_not_a_closure(tmp_path, monkeypatch):
    def run(argv, **kwargs):
        raise subprocess.CalledProcessError(1, argv, output=json.dumps(
            {"id": "req-2", "ok": False, "error": {"code": "runtime_unavailable"}}))

    monkeypatch.setattr(lane_talk.subprocess, "run", run)
    record = {"kind": "orca", "executable": "/bin/orca", "handle": "term_1"}
    assert lane_talk._alive(record, tmp_path, show=False) is None


def test_queue_uses_the_session_recorded_while_the_terminal_opened(store, tmp_path, monkeypatch):
    session(store, "codex")
    terminals = Terminals(monkeypatch)

    def during(argv):
        if argv[1:3] == ["terminal", "create"]:  # The runner records the worker's new session now.
            store.update_run(1, session_id="thread-2", session_executor="codex")

    terminals.on_call = during
    lanes.talk(1, message="hi", store=store, config=config(), repo_root=tmp_path)
    queue = next(argv for argv in terminals.calls if Path(argv[0]).name == "codex")
    assert queue[queue.index("--thread") + 1] == "thread-2"


def test_runner_session_record_waits_for_an_open_talk_command(store, tmp_path):
    import threading

    from foragent import run_execution

    assert "talk_lock" in __import__("inspect").getsource(run_execution._available_executor)
    done = threading.Event()
    with store.talk_lock(1):
        def write():
            with store.talk_lock(1):
                done.set()

        thread = threading.Thread(target=write)
        thread.start()
        assert not done.wait(0.3)  # A session write waits while a talk sends.
    assert done.wait(2)
    thread.join()


@pytest.mark.parametrize("talk_ends_first", [True, False])
def test_a_talk_overlapping_a_test_slot_wait_is_credited_once(tmp_path, monkeypatch, talk_ends_first):
    from types import SimpleNamespace

    from foragent import run_waits

    clock = [NOW]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    run = store.start_run(1, branch="foragent/issue-1", worktree=store.worktree_path(1))
    ctx = SimpleNamespace(store=store, now=lambda: clock[0], config=config(max_runtime_minutes=60))

    @__import__("contextlib").contextmanager
    def slot(slots, waiting):
        waiting()  # Queued for 40 minutes while a person talks for the same 40 minutes.
        store.begin_talk(1, run.started_at, clock[0])
        clock[0] += timedelta(minutes=40)
        if talk_ends_first:
            store.end_talk(1, clock[0])
        yield

    monkeypatch.setattr(run_waits.capacity, "hold_test_slot", slot)
    with run_waits._test_slot(1, ctx):
        pass
    store.end_talk(1, clock[0])
    clock[0] += timedelta(minutes=20)  # 60 minutes passed; 40 of them were paused once.
    assert store.get_run(1).waited_seconds == 40 * 60
    assert run_waits._remaining(1, ctx) == 40 * 60


def test_open_talk_and_open_wait_pause_the_live_budget_once(tmp_path):
    from types import SimpleNamespace

    from foragent import run_waits

    clock = [NOW]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    run = store.start_run(1, branch="foragent/issue-1", worktree=store.worktree_path(1))
    ctx = SimpleNamespace(store=store, now=lambda: clock[0], config=config(max_runtime_minutes=60))
    store.add_waited(1, 600, wait="open", key="backoff", now=clock[0])  # Credited up front.
    store.begin_talk(1, run.started_at, clock[0])
    clock[0] += timedelta(minutes=10)
    assert run_waits._remaining(1, ctx) == 60 * 60  # Both open for 10 minutes: paused 10, not 20.


def test_orca_cli_killed_by_a_signal_is_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(lane_talk.subprocess, "run", lambda argv, **kw: (_ for _ in ()).throw(
        subprocess.CalledProcessError(-9, argv)))
    record = {"kind": "orca", "executable": "/bin/orca", "handle": "term_1"}
    assert lane_talk._alive(record, tmp_path, show=False) is None


@pytest.mark.parametrize("content", ["{not json", json.dumps({"kind": "orca"}), json.dumps([1])])
def test_unreadable_talk_record_keeps_the_talk_and_opens_no_duplicate(store, tmp_path, monkeypatch, content):
    session(store, "codex")
    terminals = Terminals(monkeypatch)
    lanes.talk(1, store=store, config=config(), repo_root=tmp_path)
    lane_talk.talk_path(store, 1).write_text(content, encoding="utf-8")
    run, still_open = lane_talk.check_talking(store, store.get_run(1), tmp_path, NOW)
    assert still_open and store.get_run(1).talking is True
    with pytest.raises(lane_talk_open.TalkError):
        lanes.talk(1, store=store, config=config(), repo_root=tmp_path)
    assert len(terminals.opened()) == 1 and store.get_run(1).talking is True


def test_a_resume_that_switches_threads_never_lets_talk_send_to_the_old_one(run_case):
    from dataclasses import replace

    from foragent.executors import ExecResult
    from foragent.run_execution import _available_executor

    c = run_case
    ctx = replace(c.ctx, config=replace(c.ctx.config, executor="codex"))
    c.store.update_run(1, session_id="old-thread", session_executor="codex")
    seen = []

    class Executor:
        def run(self, **kwargs):
            if "resume_session_id" in kwargs:  # The resume is refused, so a fresh thread starts.
                return ExecResult(1, False, "resume refused", kwargs["log_path"])
            run = c.store.get_run(1)
            seen.append(run.session_stale)
            with pytest.raises(lane_talk_open.TalkError, match="new worker session"):
                lane_talk_open._current_session(c.store, run, 1, "en")  # A talk sending right now.
            return ExecResult(0, False, "done", kwargs["log_path"], session_id="new-thread", num_turns=1)

    _available_executor(1, ctx, Executor(), session_body="body", resume_prompt="continue",
                        worktree=ctx.repo_root, prompt="full task", model=None, log_path=c.store.log_path(1))
    run = c.store.get_run(1)
    assert seen == [True] and run.session_id == "new-thread" and run.session_stale is False
    assert lane_talk_open._current_session(c.store, run, 1, "en") == "new-thread"
    c.store.finish_run(1, "merged")
    c.tracker.remove_labels(1, ["running"])


def test_an_inherited_session_is_not_the_new_runs_worker_until_resumed(store):
    store.update_run(1, session_id="earlier-thread", session_executor="codex")
    store.finish_run(1, "crashed")
    run = store.start_run(1, branch="foragent/issue-1", worktree=store.worktree_path(1))
    assert run.session_id == "earlier-thread" and run.session_stale is True
    assert lane_talk_open._session(run, config())[1] is None


def test_two_overlapping_120s_backoffs_inside_a_600s_talk_credit_600s(tmp_path):
    from types import SimpleNamespace

    from foragent.run_waits import _executor_wait

    clock, mono = [NOW], [0.0]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    run = store.start_run(1, branch="foragent/issue-1", worktree=store.worktree_path(1))
    store.begin_talk(1, run.started_at, clock[0])
    clock[0] += timedelta(seconds=100)
    mono[0] += 100
    depth = []

    def sleep(seconds):
        if not depth:  # A second panel reviewer backs off while the first one waits.
            depth.append(1)
            _executor_wait(1, ctx, 120)
            return
        clock[0] += timedelta(seconds=seconds)
        mono[0] += seconds

    ctx = SimpleNamespace(store=store, now=lambda: clock[0], monotonic=lambda: mono[0], sleep=sleep)
    _executor_wait(1, ctx, 120)
    assert store.get_run(1).pauses_open == {"talk": NOW.isoformat()}
    clock[0] = NOW + timedelta(seconds=600)
    ended = store.end_talk(1, clock[0])
    assert ended.waited_seconds == 600 and ended.pauses_open == {} and ended.pause_mark is None


@pytest.mark.parametrize("listing", ["{}", json.dumps({"id": "req-4", "ok": True, "result": {}})])
def test_an_empty_or_unrecognised_orca_listing_keeps_the_talk_open(store, tmp_path, monkeypatch, listing):
    session(store, "codex")
    terminals = Terminals(monkeypatch)
    lanes.talk(1, store=store, config=config(), repo_root=tmp_path)

    def run(argv, **kwargs):
        terminals.calls.append(list(argv))
        if argv[1:3] == ["terminal", "show"]:
            raise subprocess.CalledProcessError(1, argv, output="", stderr="crashed")
        return subprocess.CompletedProcess(argv, 0, stdout=listing, stderr="")

    monkeypatch.setattr(lane_talk.subprocess, "run", run)
    run_record, still_open = lane_talk.check_talking(store, store.get_run(1), tmp_path, NOW)
    assert still_open and store.get_run(1).talking is True and lane_talk.talk_path(store, 1).exists()


@pytest.mark.parametrize("failure", [
    subprocess.CalledProcessError(1, ["orca"], output="", stderr="Traceback: crashed"),
    subprocess.CalledProcessError(1, ["orca"], output=json.dumps({"handle": "term_1"}), stderr=""),
    subprocess.CalledProcessError(1, ["orca"], output="{}", stderr=""),
    subprocess.TimeoutExpired(["orca"], 10),
])
def test_orca_closes_only_on_its_structured_stale_or_missing_answer(tmp_path, monkeypatch, failure):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv[1:3])
        raise failure

    monkeypatch.setattr(lane_talk.subprocess, "run", run)
    record = {"kind": "orca", "executable": "/bin/orca", "handle": "term_1"}
    assert lane_talk._alive(record, tmp_path, show=False) is None
    assert calls == [["terminal", "show"]]  # No second guess from a listing.


@pytest.mark.parametrize("code", ["terminal_handle_stale", "terminal_not_found"])
@pytest.mark.parametrize("exit_code", [0, 1])
def test_orca_stale_or_missing_envelope_on_stdout_closes_with_any_exit_code(tmp_path, monkeypatch, code, exit_code):
    gone = json.dumps({"id": "req-5", "ok": False, "error": {"code": code}})

    def run(argv, **kwargs):
        if exit_code:
            raise subprocess.CalledProcessError(exit_code, argv, output=gone)
        return subprocess.CompletedProcess(argv, 0, stdout=gone, stderr="")

    monkeypatch.setattr(lane_talk.subprocess, "run", run)
    record = {"kind": "orca", "executable": "/bin/orca", "handle": "term_1"}
    assert lane_talk._alive(record, tmp_path, show=False) is False


def test_tmux_closes_only_on_a_working_reply_on_the_recorded_socket(tmp_path, monkeypatch):
    replies = []
    monkeypatch.setattr(lane_talk.subprocess, "run", lambda argv, **kw: replies.pop(0)(argv))
    ok = lambda stdout: lambda argv: subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")  # noqa: E731

    def fail(argv):
        raise subprocess.CalledProcessError(1, argv, stderr="no server running on /tmp/sock")

    with_socket = {"kind": "tmux", "executable": "/bin/tmux", "socket": "/tmp/sock", "handle": "@7"}
    no_socket = {"kind": "tmux", "executable": "/bin/tmux", "handle": "@7"}
    replies[:] = [fail]
    assert lane_talk._alive(with_socket, tmp_path, show=False) is None  # A failing server tells nothing.
    replies[:] = [ok("@1\n@7\n")]
    assert lane_talk._alive(with_socket, tmp_path, show=False) is True
    replies[:] = [ok("@1\n")]
    assert lane_talk._alive(with_socket, tmp_path, show=False) is False  # Working reply without the window.
    replies[:] = [ok("@1\n")]
    assert lane_talk._alive(no_socket, tmp_path, show=False) is None  # Another server may have answered.
