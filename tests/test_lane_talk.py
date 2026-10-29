"""Boundary cases of `fa lanes talk` (D55 addendum, D95) with fake terminals and no real processes."""

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from foragent import lane_talk, lane_talk_open, lane_talk_probe as probe, lanes
from foragent.config import Config
from foragent.orch_events import append_event
from foragent.state import StateStore
from runner_support import run_case  # noqa: F401 - the shared runner fixture

NOW = datetime(2026, 10, 27, 12, tzinfo=UTC)
ORCA_OPEN = json.dumps({"id": "req-1", "ok": True, "result": {"terminal": {"handle": "term_1"}}})
ORCA_STALE = json.dumps({"id": "req-9", "ok": False, "error": {"code": "terminal_handle_stale"}})


class Terminals:
    """Fake openers: Orca answers in its real envelope, tmux on a real socket path."""

    def __init__(self, monkeypatch, *, opener="orca", queue_fails=False, alive=True, socket=None):
        self.calls, self.alive, self.on_call, self.switch_fails, self.listing = [], alive, None, False, None
        for key in ("ORCA_TERMINAL_HANDLE", "TMUX"):
            monkeypatch.delenv(key, raising=False)
        if opener == "orca":
            monkeypatch.setenv("ORCA_TERMINAL_HANDLE", "source")
        if opener == "tmux":
            monkeypatch.setenv("TMUX", f"{socket},4242,0")
        available = {"codex", "claude", "orca", opener} if opener != "none" else {"codex", "claude"}
        if opener in {"tmux", "osascript", "none"}:
            available.discard("orca")
        if opener == "tmux":
            available.add("tmux")
        monkeypatch.setattr(shutil, "which", lambda name: f"/bin/{name}" if name in available else None)

        def run(argv, **kwargs):
            self.calls.append(list(argv))
            if self.on_call is not None:
                self.on_call(list(argv))
            name = Path(argv[0]).name
            if name == "codex" and queue_fails:
                raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))
            probing = argv[1:3] == ["terminal", "show"] or ("list-windows" in argv and "#{window_id}" in argv)
            if probing and self.alive == "timeout":
                raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))
            if argv[1:3] == ["terminal", "list"] or "#{window_id}\t#{window_name}" in argv:
                if self.listing is None:
                    raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))
                return subprocess.CompletedProcess(argv, 0, stdout=self.listing, stderr="")
            if probing and not self.alive:
                if name == "orca":
                    raise subprocess.CalledProcessError(1, argv, output=ORCA_STALE)
                return subprocess.CompletedProcess(argv, 0, stdout="@1\n", stderr="")
            if probing and name == "tmux":
                return subprocess.CompletedProcess(argv, 0, stdout="@1\n@7\n", stderr="")
            if argv[1:3] == ["terminal", "switch"] or "select-window" in argv:
                return subprocess.CompletedProcess(argv, 1 if self.switch_fails else 0, stdout="", stderr="")
            stdout = {"orca": ORCA_OPEN, "tmux": "@7\n"}.get(name, "")
            return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

        monkeypatch.setattr(subprocess, "run", run)

    def opened(self):
        return [argv for argv in self.calls if argv[1:3] == ["terminal", "create"] or "new-window" in argv]


@pytest.fixture
def store(tmp_path):
    store = StateStore(tmp_path / "state", now=lambda: NOW)
    store.start_run(1, branch="foragent/issue-1", worktree=store.worktree_path(1), pid=None)
    return store


@pytest.fixture
def socket(tmp_path):
    path = tmp_path / "tmux-socket"
    path.write_text("")
    return path


def session(store, executor, status="running"):
    if status != "running":
        store.finish_run(1, "blocked")
    path = store.root / "runs/1.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record.update(session_id="thread-1", session_executor=executor)
    path.write_text(json.dumps(record), encoding="utf-8")


def config(**values):
    return Config(**{"executor": "codex", "repo": "owner/repo", "language": "en", **values})


def talk_event(store, at=NOW, **fields):
    """The talk-opened event `fa lanes talk` appends (default: an Orca handle)."""
    fields = fields or {"handle": "term_1"}
    return append_event(store, at, 1, "talk-opened", "Talk opened for #1 (codex)",
                        run_started_at=store.get_run(1).started_at, executor="codex", **fields)


def events(store, kind):
    path = store.root / "orch-events.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    return [row for row in rows if row["kind"] == kind]


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


def test_blocked_message_becomes_first_resume_prompt(store, tmp_path, monkeypatch):
    session(store, "codex", status="blocked")
    terminals = Terminals(monkeypatch)
    lanes.talk(1, message="Why blocked?", store=store, config=config(), repo_root=tmp_path)
    command = terminals.opened()[0]
    assert "resume thread-1 'Why blocked?'" in command[command.index("--command") + 1]
    assert not any(Path(argv[0]).name == "codex" for argv in terminals.calls)


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


def test_queue_uses_the_session_recorded_while_the_terminal_opened(store, tmp_path, monkeypatch):
    session(store, "codex")
    terminals = Terminals(monkeypatch)
    lanes.talk(1, store=store, config=config(), repo_root=tmp_path)  # The log terminal opens.
    store.update_run(1, session_id="thread-2", session_executor="codex")  # The worker moves on meanwhile.
    lanes.talk(1, message="hi", store=store, config=config(), repo_root=tmp_path)  # Sent while it is open.
    queue = next(argv for argv in terminals.calls if Path(argv[0]).name == "codex")
    assert queue[queue.index("--thread") + 1] == "thread-2" and len(terminals.opened()) == 1


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



def test_failed_queue_exits_nonzero_keeps_log_open_and_records_nothing(store, tmp_path, monkeypatch):
    session(store, "codex")
    terminals = Terminals(monkeypatch, queue_fails=True)
    with pytest.raises(lane_talk_open.TalkError):
        lanes.talk(1, message="hi", store=store, config=config(), repo_root=tmp_path)
    # The send runs first: when it fails nothing is opened or written (the narrowed decision).
    assert terminals.opened() == []
    assert events(store, "talk-opened") == [] and store.get_run(1).talking is False


def test_tmux_records_window_id_and_reshows_instead_of_opening_again(store, tmp_path, monkeypatch, socket):
    session(store, "codex")
    terminals = Terminals(monkeypatch, opener="tmux", socket=socket)
    lanes.talk(1, store=store, config=config(), repo_root=tmp_path)
    (event,) = events(store, "talk-opened")
    assert event["socket"] == str(socket) and event["window_id"] == "@7" and store.get_run(1).talking
    line = lanes.talk(1, store=store, config=config(), repo_root=tmp_path)
    assert "Showed" in line and len(terminals.opened()) == 1
    assert ["/bin/tmux", "-S", str(socket), "select-window", "-t", "@7"] in terminals.calls
    assert len(events(store, "talk-opened")) == 1  # Bringing forward appends nothing.


def test_terminal_app_has_no_handle_so_talking_stays_unset(store, tmp_path, monkeypatch):
    session(store, "codex")
    Terminals(monkeypatch, opener="osascript")
    line = lanes.talk(1, store=store, config=config(), repo_root=tmp_path)
    assert "stall checks keep running" in line
    assert store.get_run(1).talking is False and events(store, "talk-opened") == []


def test_closed_terminal_adds_paused_time_and_restarts_stall_clock(store, tmp_path, monkeypatch):
    session(store, "codex")
    terminals = Terminals(monkeypatch)
    lanes.talk(1, store=store, config=config(), repo_root=tmp_path)
    run = store.get_run(1)
    assert run.talking and run.talking_since == NOW.isoformat()
    run, still = lane_talk.check_talking(store, run, tmp_path, NOW + timedelta(minutes=5))
    assert still and events(store, "talk-closed") == []
    terminals.alive = False
    later = NOW + timedelta(minutes=9)
    run, still = lane_talk.check_talking(store, store.get_run(1), tmp_path, later)
    # The talk's 9 minutes are derived from its events; waited_seconds keeps only runner waits.
    assert not still and run.talking is False and run.waited_seconds == 0
    assert run.talk_pause_seconds(later + timedelta(hours=1)) == 9 * 60
    (closed,) = events(store, "talk-closed")
    assert closed["handle"] == "term_1" and closed["summary"] == "Talk closed for #1 after 9 min"
    assert run.last_progress_at == run.started_at  # Not moved; the stall clock derives from the event.
    assert lane_talk.stall_since(store, run) == later


def test_finishing_a_talking_run_clears_talk_state(store, tmp_path, monkeypatch):
    session(store, "codex")
    Terminals(monkeypatch)
    lanes.talk(1, store=store, config=config(), repo_root=tmp_path)
    store.finish_run(1, "merged")
    run = store.get_run(1)
    # The finish ends the talk: no talk-closed event, talking derived false, the pause up to finished_at.
    assert run.talking is False and events(store, "talk-closed") == []
    assert run.talk_pause_seconds(NOW + timedelta(hours=1)) == 0  # Started and finished at NOW.


def test_runtime_budget_excludes_an_open_talk_and_extends_the_executor_deadline(store):
    from types import SimpleNamespace

    from foragent import processes
    from foragent.run_waits import _remaining

    assert store.get_run(1).started_at == NOW.isoformat()
    talk_event(store, NOW + timedelta(minutes=10))
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
    assert process.waits == [10, 30.0, 20.0]
    with processes.extend_deadline(lambda: 5.0), pytest.raises(subprocess.TimeoutExpired):
        processes._wait_with_deadline(Process(), 10)


def test_talk_credit_and_runner_wait_credit_both_count(store):
    event = talk_event(store)
    store.add_waited(1, 30)  # A runner credit while the talk is open.
    store.close_talk(1, event, NOW + timedelta(minutes=2))
    store.add_waited(1, 15)
    run = store.get_run(1)
    assert run.waited_seconds == 45 and run.talk_pause_seconds(NOW + timedelta(hours=1)) == 120


def test_runner_extends_every_command_timeout_by_the_runs_pause(store, monkeypatch):
    from types import SimpleNamespace

    from foragent import processes, runner
    from foragent.tracker import FakeTracker

    seen = []
    monkeypatch.setattr(runner, "_execute", lambda n, ctx, worktree: seen.append(
        processes._paused_seconds.get()()) or "merged")
    monkeypatch.setattr(runner.board, "sync_status", lambda *a, **kw: None)
    monkeypatch.setattr(runner, "kick_tick", lambda root: None)
    store.update_run(1, waited_seconds=12)
    talk_event(store, NOW)  # Open since the run began; the run's clock is 30 s later.
    later = NOW + timedelta(seconds=30)
    ctx = runner.RunContext("owner/repo", Path("."), FakeTracker(now=lambda: later), store, config(),
                            SimpleNamespace(remove_worktree=lambda path: None), now=lambda: later)
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
    assert store.get_run(1).talking is False  # The tick's read.
    event = talk_event(store)  # fa lanes talk opens before the stop acts.
    assert scheduler_recovery._kill_run(ctx, 1, report, count_stall=True, unless_talking=True) is False
    assert stops == [] and store.get_run(1).restarts == 0
    store.close_talk(1, event, NOW + timedelta(minutes=1))
    scheduler_recovery._kill_run(ctx, 1, report, unless_talking=True)  # No talk: the stop goes ahead.
    assert stops == [1]


def test_tmux_talk_targets_its_own_server_socket_from_the_tick(store, tmp_path, monkeypatch, socket):
    session(store, "codex")
    terminals = Terminals(monkeypatch, opener="tmux", socket=socket)
    lanes.talk(1, store=store, config=config(), repo_root=tmp_path)
    create = terminals.opened()[0]
    assert create[1:4] == ["-S", str(socket), "new-window"]
    monkeypatch.delenv("TMUX")  # The gate tick runs outside tmux.
    _, still = lane_talk.check_talking(store, store.get_run(1), tmp_path, NOW)
    assert still and terminals.calls[-1][1:4] == ["-S", str(socket), "list-windows"]


def test_finishing_after_a_ten_minute_talk_credits_600_waited_seconds(tmp_path):
    clock = [NOW]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    store.start_run(1, branch="foragent/issue-1", worktree=store.worktree_path(1))
    talk_event(store)
    clock[0] = NOW + timedelta(minutes=10)
    store.finish_run(1, "merged")
    clock[0] = NOW + timedelta(hours=2)
    run = store.get_run(1)
    # Derived, not written: the unclosed talk counts up to the run's finish time.
    assert run.waited_seconds == 0 and run.talk_pause_seconds(clock[0]) == 600 and run.talking is False


def test_orca_handle_comes_from_the_result_terminal_not_the_request_id(store, tmp_path, monkeypatch):
    session(store, "codex")
    terminals = Terminals(monkeypatch)
    lanes.talk(1, store=store, config=config(), repo_root=tmp_path)
    assert events(store, "talk-opened")[0]["handle"] == "term_1"
    lanes.talk(1, store=store, config=config(), repo_root=tmp_path)  # The same terminal is re-shown.
    assert len(terminals.opened()) == 1
    assert probe.created_handle(json.dumps({"id": "req-1", "ok": False, "error": {"code": "x"}})) is None


@pytest.mark.parametrize("opener", ["orca", "tmux"])
def test_a_probe_that_cannot_answer_keeps_the_talk(store, tmp_path, monkeypatch, opener, socket):
    session(store, "codex")
    terminals = Terminals(monkeypatch, opener=opener, socket=socket)
    lanes.talk(1, store=store, config=config(), repo_root=tmp_path)
    terminals.alive = "timeout"
    _, still = lane_talk.check_talking(store, store.get_run(1), tmp_path, NOW)
    assert still and store.get_run(1).talking is True and events(store, "talk-closed") == []
    with pytest.raises(lane_talk_open.TalkError, match="Cannot tell"):
        lanes.talk(1, store=store, config=config(), repo_root=tmp_path)  # Never doubled, nothing shown.
    assert len(terminals.opened()) == 1
    terminals.alive = False
    _, still = lane_talk.check_talking(store, store.get_run(1), tmp_path, NOW)
    assert not still and store.get_run(1).talking is False


def test_orca_error_other_than_a_stale_handle_is_not_a_closure(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(
        argv, 1, json.dumps({"ok": False, "error": {"code": "runtime_unavailable"}}), ""))
    assert probe.liveness({"handle": "term_1"}, tmp_path) == probe.UNKNOWN


@pytest.mark.parametrize("talk_ends_first", [True, False])
def test_a_talk_overlapping_a_test_slot_wait_is_credited_once(tmp_path, monkeypatch, talk_ends_first):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from foragent import run_waits

    clock = [NOW]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    store.start_run(1, branch="foragent/issue-1", worktree=store.worktree_path(1))
    ctx = SimpleNamespace(store=store, now=lambda: clock[0], config=config(max_runtime_minutes=60))
    talk = []

    @contextmanager
    def slot(slots, waiting):
        waiting()  # Queued for 40 minutes while a person talks for the same 40 minutes.
        talk.append(talk_event(store, clock[0]))
        clock[0] += timedelta(minutes=40)
        if talk_ends_first:
            store.close_talk(1, talk[0], clock[0])
        yield

    monkeypatch.setattr(run_waits.capacity, "hold_test_slot", slot)
    with run_waits._test_slot(1, ctx):
        pass
    store.close_talk(1, talk[0], clock[0])
    clock[0] += timedelta(minutes=20)  # 60 minutes passed; 40 of them were paused once.
    assert store.get_run(1).waited_seconds == 40 * 60
    assert run_waits._remaining(1, ctx) == 40 * 60


def test_open_talk_and_open_wait_pause_the_live_budget_once(tmp_path):
    from types import SimpleNamespace

    from foragent import run_waits

    clock = [NOW]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    store.start_run(1, branch="foragent/issue-1", worktree=store.worktree_path(1))
    ctx = SimpleNamespace(store=store, now=lambda: clock[0], config=config(max_runtime_minutes=60))
    store.add_waited(1, key="backoff", start=clock[0], end=clock[0] + timedelta(seconds=600))  # Up front.
    talk_event(store, clock[0])
    clock[0] += timedelta(minutes=10)
    assert run_waits._remaining(1, ctx) == 60 * 60  # Both open for 10 minutes: paused 10, not 20.


def test_orca_cli_killed_by_a_signal_is_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: (_ for _ in ()).throw(
        subprocess.CalledProcessError(-9, argv)))
    assert probe.liveness({"handle": "term_1"}, tmp_path) == probe.UNKNOWN


def test_two_overlapping_120s_backoffs_inside_a_600s_talk_credit_600s(tmp_path):
    from types import SimpleNamespace

    from foragent.run_waits import _executor_wait

    clock, mono = [NOW], [0.0]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    store.start_run(1, branch="foragent/issue-1", worktree=store.worktree_path(1))
    event = talk_event(store)
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
    run = store.get_run(1)
    assert run.waited_seconds == 120 and run.pause_regions == {} and len(run.pause_closed) == 1
    ended = store.close_talk(1, event, NOW + timedelta(seconds=600))
    later = NOW + timedelta(hours=1)
    assert ended.waited_seconds + ended.talk_pause_seconds(later) == 600  # The union, counted once.


@pytest.mark.parametrize("listing", ["{}", json.dumps({"id": "req-4", "ok": True, "result": {}})])
def test_an_empty_or_unrecognised_orca_listing_keeps_the_talk_open(store, tmp_path, monkeypatch, listing):
    terminals = Terminals(monkeypatch)
    terminals.listing = listing
    talk_event(store, title="talk #1 deadbeef")
    _, still = lane_talk.check_talking(store, store.get_run(1), tmp_path, NOW)
    assert still and store.get_run(1).talking is True and events(store, "talk-closed") == []


@pytest.mark.parametrize("reply", [
    subprocess.CompletedProcess(["orca"], 1, "", "Traceback: crashed"),
    subprocess.CompletedProcess(["orca"], 1, json.dumps({"handle": "term_1"}), ""),
    subprocess.CompletedProcess(["orca"], 1, "{}", ""),
    subprocess.TimeoutExpired(["orca"], 10),
])
def test_orca_closes_only_on_its_structured_stale_or_missing_answer(tmp_path, monkeypatch, reply):
    monkeypatch.setattr(shutil, "which", lambda name: f"/bin/{name}")

    def run(argv, **kwargs):
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(subprocess, "run", run)
    assert probe.liveness({"handle": "term_1"}, tmp_path) == probe.UNKNOWN


@pytest.mark.parametrize("code", ["terminal_handle_stale", "terminal_not_found"])
@pytest.mark.parametrize("exit_code", [0, 1])
@pytest.mark.parametrize("ok", [False, True])
def test_orca_stale_or_missing_envelope_on_stdout_closes_with_any_exit_code(tmp_path, monkeypatch, code, exit_code, ok):
    monkeypatch.setattr(shutil, "which", lambda name: f"/bin/{name}")
    gone = json.dumps({"id": "req-5", "ok": ok, "error": {"code": code}})
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, exit_code, gone, ""))
    assert probe.liveness({"handle": "term_1"}, tmp_path) == probe.CLOSED


def test_tmux_closes_only_on_a_working_reply_on_the_recorded_socket(tmp_path, monkeypatch, socket):
    monkeypatch.setattr(shutil, "which", lambda name: f"/bin/{name}")
    replies = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: replies.pop(0))
    event = {"socket": str(socket), "window_id": "@7"}
    for reply, expected in [((1, ""), probe.UNKNOWN), ((0, "@1\n@7\n"), probe.OPEN), ((0, "@1\n"), probe.CLOSED)]:
        replies[:] = [subprocess.CompletedProcess([], reply[0], reply[1], "")]
        assert probe.liveness(event, tmp_path) == expected
    assert probe.liveness({"window_id": "@7"}, tmp_path) == probe.UNKNOWN  # No socket saved.


def test_a_talk_whose_command_died_before_saving_the_handle_is_found_by_title(store, tmp_path, monkeypatch):
    session(store, "codex")
    terminals = Terminals(monkeypatch)
    terminals.listing = json.dumps({"ok": True, "result": {"terminals": [
        {"handle": "term_1", "title": "talk #1 deadbeef"}, {"handle": "term_2", "title": "other"}]}})
    pending = talk_event(store, title="talk #1 deadbeef")
    _, still = lane_talk.check_talking(store, store.get_run(1), tmp_path, NOW)
    assert still and ["/bin/orca", "terminal", "show", "--terminal", "term_1", "--json"] in terminals.calls
    assert len(events(store, "talk-opened")) == 1  # The tick uses the found handle for its probe only.
    lanes.talk(1, store=store, config=config(), repo_root=tmp_path)  # Brought forward, never doubled.
    assert terminals.opened() == []
    newest = events(store, "talk-opened")[-1]
    assert newest["handle"] == "term_1" and newest is not pending  # The next talk saves the handle.


def test_pending_talk_with_an_unanswered_listing_stays_open_and_is_not_doubled(store, tmp_path, monkeypatch):
    session(store, "codex")
    terminals = Terminals(monkeypatch)  # Its listing times out.
    talk_event(store, title="talk #1 deadbeef")
    _, still = lane_talk.check_talking(store, store.get_run(1), tmp_path, NOW)
    assert still and store.get_run(1).talking is True
    with pytest.raises(lane_talk_open.TalkError, match="Cannot tell"):
        lanes.talk(1, store=store, config=config(), repo_root=tmp_path)
    assert terminals.opened() == []


@pytest.mark.parametrize("failure", [
    subprocess.CalledProcessError(-9, ["orca"]),
    subprocess.CalledProcessError(1, ["orca"], output=json.dumps({"ok": False, "error": {"code": "x"}})),
    subprocess.TimeoutExpired(["orca"], 10),
])
def test_a_failed_open_reply_keeps_the_pending_talk(store, tmp_path, monkeypatch, failure):
    session(store, "codex")
    terminals = Terminals(monkeypatch)

    def fail(argv):
        if argv[1:3] == ["terminal", "create"]:
            raise failure  # The terminal may already exist when the reply fails.

    terminals.on_call = fail
    with pytest.raises(lane_talk_open.TalkError):
        lanes.talk(1, store=store, config=config(), repo_root=tmp_path)
    (event,) = events(store, "talk-opened")
    assert store.get_run(1).talking is True and "handle" not in event and event["title"].startswith("talk #1 ")


def test_an_opener_that_cannot_start_ends_the_talk(store, tmp_path, monkeypatch):
    session(store, "codex")
    terminals = Terminals(monkeypatch)

    def missing(argv):
        if argv[1:3] == ["terminal", "create"]:
            raise FileNotFoundError(argv[0])  # Nothing ran, so nothing opened.

    terminals.on_call = missing
    with pytest.raises(FileNotFoundError):
        lanes.talk(1, store=store, config=config(), repo_root=tmp_path)
    assert store.get_run(1).talking is False and events(store, "talk-opened") == []


def test_tmux_pending_lookup_without_the_title_is_not_a_closure(tmp_path, monkeypatch, socket):
    monkeypatch.setattr(shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(
        argv, 0, "@1\tother\n", ""))
    event = {"socket": str(socket), "title": "talk #1 deadbeef"}
    assert probe.find_pending(event, tmp_path) is None and probe.resolved(event, tmp_path) is None


LIVENESS_ORCA = [
    (0, ORCA_OPEN, "open"),
    (0, json.dumps({"handle": "term_1", "terminal": "term_1", "id": "term_1"}), "open"),  # The locked fake.
    (0, json.dumps({"result": {"id": "term_1"}}), "open"),  # A result key without ok is an envelope.
    (0, json.dumps({"ok": True, "result": {"terminal": {"id": "term_1"}}}), "open"),
    (0, json.dumps({"ok": True}), "unknown"),  # An envelope missing its result.
    (0, json.dumps({"ok": True, "result": ["term_1"]}), "unknown"),
    (0, json.dumps({"ok": True, "result": {"handle": "term_2"}}), "unknown"),
    (0, json.dumps({"handle": ""}), "unknown"),
    (0, "not json", "unknown"),
    (0, "", "unknown"),
    (1, ORCA_STALE, "closed"),
    (0, ORCA_STALE, "closed"),
    (1, json.dumps({"ok": False, "error": {"code": "terminal_not_found"}}), "closed"),
    (1, json.dumps({"ok": False, "error": {"code": "other"}}), "unknown"),
    (2, ORCA_OPEN, "unknown"),
]
LIVENESS_TMUX = [
    (0, "@1\n@7\n", "@7", "open"),
    (0, "@1\r\n@7  \n", "@7", "open"),
    (0, "@1\n", "@7", "closed"),
    (0, "@1\n\n@3\n", "@7", "closed"),
    (0, "not-a-window-list", "@7", "unknown"),
    (0, "@1\nnot-a-window\n", "@7", "unknown"),
    (0, "", "@7", "unknown"),
    (1, "@1\n", "@7", "unknown"),
    (0, "@1\n", "7", "unknown"),  # A recorded id that is not a window id.
]


@pytest.mark.parametrize("exit_code,stdout,expected", LIVENESS_ORCA)
def test_liveness_table_for_orca_replies(tmp_path, monkeypatch, exit_code, stdout, expected):
    monkeypatch.setattr(shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, exit_code, stdout, ""))
    assert probe.liveness({"handle": "term_1"}, tmp_path) == expected


@pytest.mark.parametrize("exit_code,stdout,window,expected", LIVENESS_TMUX)
def test_liveness_table_for_tmux_replies(tmp_path, monkeypatch, socket, exit_code, stdout, window, expected):
    monkeypatch.setattr(shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, exit_code, stdout, ""))
    assert probe.liveness({"socket": str(socket), "window_id": window}, tmp_path) == expected
    assert probe.liveness({"socket": str(socket.with_name("gone")), "window_id": window}, tmp_path) == "unknown"


FIND_ORCA = [
    (0, {"ok": True, "result": {"terminals": [{"handle": "term_1", "title": "T"}]}}, "term_1"),
    (0, {"terminals": [{"id": "term_1", "title": "T"}]}, "term_1"),
    (0, [{"terminal": {"handle": "term_1"}, "name": "T"}], "term_1"),
    (0, [{"title": "other", "name": "T", "handle": "term_1"}], None),  # Title wins over name.
    (0, [{"handle": "term_1", "title": "T"}, {"handle": "term_2", "title": "T"}], None),  # Not exactly one.
    (0, [{"title": "T"}, {"handle": "term_2", "title": "T"}], "term_2"),  # A match without a handle is skipped.
    (0, [{"handle": "x"}, {"handle": "term_1", "title": "T"}], "term_1"),  # No title or name: skipped.
    (0, [{"handle": "term_1", "title": "T"}, "junk"], None),  # A non-dict entry: malformed.
    (0, [{"handle": "term_1", "title": 3}], None),
    (0, {}, None),
    (0, {"ok": True, "result": {}}, None),
    (1, [{"handle": "term_1", "title": "T"}], None),
]
FIND_TMUX = [
    (0, "@1\tother\n@7\tT\n", "@7"),
    (0, "@7\r\tT\n", "@7"),  # Only the window-id field loses its trailing \r.
    (0, "@7\tT\r\n", None),  # The name is compared raw: "T\r" is not "T".
    (0, "@7\tT\tmore\n", None),  # The name after the first tab is "T\tmore".
    (0, "@7\tT\n@8\tT\n", None),
    (0, "@7 T\n", None),  # A line without a tab is malformed.
    (0, "x7\tT\n", None),
    (1, "@7\tT\n", None),
]


@pytest.mark.parametrize("exit_code,listing,expected", FIND_ORCA)
def test_find_pending_table_for_orca_listings(tmp_path, monkeypatch, exit_code, listing, expected):
    monkeypatch.setattr(shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(
        argv, exit_code, json.dumps(listing), ""))
    assert probe.find_pending({"title": "T"}, tmp_path) == expected


@pytest.mark.parametrize("exit_code,listing,expected", FIND_TMUX)
def test_find_pending_table_for_tmux_listings(tmp_path, monkeypatch, socket, exit_code, listing, expected):
    monkeypatch.setattr(shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, exit_code, listing, ""))
    assert probe.find_pending({"socket": str(socket), "title": "T"}, tmp_path) == expected
    assert probe.find_pending({"socket": None, "title": "T"}, tmp_path) is None  # No saved socket.


def _cli(store, monkeypatch, tmp_path, *argv, executor="codex"):
    from foragent import cli

    monkeypatch.setattr("foragent.commands.repo_store", lambda root: (config(executor=executor), "owner/repo", store))
    return cli.main(["lanes", "talk", "1", "--repo-root", str(tmp_path), *argv])


def test_unknown_terminal_delivers_the_message_then_exits_one_with_nothing_opened(
    store, tmp_path, monkeypatch, capsys,
):
    session(store, "claude")
    terminals = Terminals(monkeypatch)
    terminals.alive = "timeout"
    talk_event(store)
    assert _cli(store, monkeypatch, tmp_path, "--message", "Keep labels.", executor="claude") == 1
    out = capsys.readouterr()
    assert "next attempt" in out.out and "Cannot tell" in out.err
    assert (store.root / "hints/1.txt").read_text() == "Keep labels." and terminals.opened() == []
    assert not any(argv[1:3] == ["terminal", "switch"] for argv in terminals.calls)


def test_bring_forward_failure_exits_one_and_leaves_the_talk_open(store, tmp_path, monkeypatch, capsys):
    session(store, "codex")
    terminals = Terminals(monkeypatch)
    talk_event(store)
    terminals.switch_fails = True
    assert _cli(store, monkeypatch, tmp_path) == 1
    assert "Could not bring" in capsys.readouterr().err
    assert store.get_run(1).talking is True and terminals.opened() == []


def test_a_newer_talk_supersedes_the_older_one_closed_at_its_time(store, tmp_path, monkeypatch):
    Terminals(monkeypatch)
    older = talk_event(store, NOW, handle="term_old")
    talk_event(store, NOW + timedelta(minutes=3), handle="term_1")
    run = store.get_run(1)
    assert run.talking_since == (NOW + timedelta(minutes=3)).isoformat()
    lane_talk.check_talking(store, run, tmp_path, NOW + timedelta(minutes=5))
    (closed,) = events(store, "talk-closed")
    assert closed["handle"] == older["handle"] and closed["at"] == (NOW + timedelta(minutes=3)).isoformat()
    run = store.get_run(1)
    assert run.talking is True and run.waited_seconds == 0
    assert run.talk_pause_seconds(NOW + timedelta(minutes=5)) == 300  # Both talks, counted once.


def test_stored_talking_and_old_talk_files_are_ignored(store):
    path = store.root / "runs/1.json"
    record = json.loads(path.read_text())
    record.update(talking=True, talking_since=NOW.isoformat(), talk_slept_base=5)
    path.write_text(json.dumps(record))
    (store.root / "talk").mkdir()
    (store.root / "talk/1.json").write_text(json.dumps({"handle": "term_1"}))
    run = store.get_run(1)
    assert run.talking is False and run.talking_since is None
    store.update_run(1, detail="still not stored")
    assert "talking" not in json.loads(path.read_text()) or json.loads(path.read_text())["talking"] is True


def test_talk_opened_event_carries_its_fields_and_summary(store, tmp_path, monkeypatch, socket):
    session(store, "codex")
    Terminals(monkeypatch, opener="tmux", socket=socket)
    lanes.talk(1, store=store, config=config(), repo_root=tmp_path)
    (event,) = events(store, "talk-opened")
    assert event["summary"] == "Talk opened for #1 (codex)" and event["issue"] == 1
    assert set(event) == {"at", "issue", "kind", "summary", "run_started_at", "executor", "mode", "socket", "window_id"}
    assert event["mode"] == "live"


def test_an_open_talk_over_an_uncredited_open_wait_still_pauses(tmp_path):
    clock = [NOW]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    store.start_run(1, branch="foragent/issue-1", worktree=store.worktree_path(1))
    store.add_waited(1, key="slot", start=NOW, end=NOW)  # A test-slot queue credited only at its start.
    talk_event(store)
    clock[0] = NOW + timedelta(seconds=600)
    run = store.get_run(1)
    assert run.waited_seconds + run.talk_pause_seconds(clock[0]) == 600  # Not zero while talking.
    store.add_waited(1, key="slot", start=NOW, end=clock[0], close=True)  # The queue ends and credits.
    run = store.get_run(1)
    assert run.waited_seconds + run.talk_pause_seconds(clock[0]) == 600  # Still once.


def test_an_overlap_finished_before_any_tick_counts_once(tmp_path):
    store = StateStore(tmp_path / "state", now=lambda: NOW)
    store.start_run(1, branch="foragent/issue-1", worktree=store.worktree_path(1))
    event = talk_event(store)  # Talk 0-60 s.
    store.add_waited(1, key="wait", start=NOW + timedelta(seconds=10), end=NOW + timedelta(seconds=20), close=True)
    store.close_talk(1, event, NOW + timedelta(seconds=60))
    run = store.get_run(1)
    assert run.waited_seconds + run.talk_pause_seconds(NOW + timedelta(hours=1)) == 60


def test_replacing_a_talk_never_lowers_the_live_pause(store):
    talk_event(store, NOW, handle="term_old")
    later = NOW + timedelta(seconds=600)
    before = store.get_run(1).talk_pause_seconds(later)
    talk_event(store, later, handle="term_1")  # Superseded before any tick closed the old one.
    assert before == 600 and store.get_run(1).talk_pause_seconds(later) == 600
    assert store.get_run(1).talk_pause_seconds(later + timedelta(seconds=60)) == 660


def test_a_second_talk_waits_for_the_first_and_reshows_its_terminal(store, tmp_path, monkeypatch):
    import threading

    session(store, "codex")
    terminals = Terminals(monkeypatch)
    sending, release, second = threading.Event(), threading.Event(), []

    def slow_queue(argv):
        if Path(argv[0]).name == "codex" and not sending.is_set():
            sending.set()
            assert release.wait(5)

    terminals.on_call = slow_queue
    first = threading.Thread(target=lambda: lanes.talk(1, message="hi", store=store, config=config(),
                                                       repo_root=tmp_path))
    first.start()
    assert sending.wait(5)
    # The send runs before the lock, so a second talk opens the log meanwhile.
    second.append(lanes.talk(1, store=store, config=config(), repo_root=tmp_path))
    release.set()
    first.join(5)
    # The first then decides under the lock, sees that terminal and re-shows it.
    assert len(terminals.opened()) == 1 and len(events(store, "talk-opened")) == 1


def test_a_non_string_name_beside_a_matching_title_makes_the_listing_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: f"/bin/{name}")
    listing = [{"title": "T", "name": 42, "handle": "h"}]
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(
        argv, 0, json.dumps(listing), ""))
    assert probe.find_pending({"title": "T"}, tmp_path) is None


def test_a_live_log_left_open_after_the_run_is_never_shown_as_its_resume_terminal(store, tmp_path, monkeypatch):
    session(store, "codex")
    terminals = Terminals(monkeypatch)
    lanes.talk(1, store=store, config=config(), repo_root=tmp_path)  # The live log, still open.
    store.finish_run(1, "blocked")
    lanes.talk(1, store=store, config=config(), repo_root=tmp_path)
    create = terminals.opened()[-1]
    assert len(terminals.opened()) == 2 and "resume thread-1" in create[create.index("--command") + 1]


def test_a_finished_issues_message_opens_a_new_resume_terminal_with_it(store, tmp_path, monkeypatch):
    session(store, "codex", status="blocked")
    terminals = Terminals(monkeypatch)
    lanes.talk(1, store=store, config=config(), repo_root=tmp_path)  # A resume terminal, still open.
    lanes.talk(1, message="Why blocked?", store=store, config=config(), repo_root=tmp_path)
    assert len(terminals.opened()) == 2
    command = terminals.opened()[-1]
    assert "resume thread-1 'Why blocked?'" in command[command.index("--command") + 1]


def test_the_message_is_sent_before_the_log_opens_and_the_event_follows_the_launch(store, tmp_path, monkeypatch):
    session(store, "codex")
    terminals = Terminals(monkeypatch)
    seen = []

    def watch(argv):
        if Path(argv[0]).name == "codex":  # No terminal is open, untracked, during the send.
            seen.append((len(terminals.opened()), len(events(store, "talk-opened"))))

    terminals.on_call = watch
    lanes.talk(1, message="hi", store=store, config=config(), repo_root=tmp_path)
    assert seen == [(0, 0)] and len(terminals.opened()) == 1 and store.get_run(1).talking is True


def _held_elsewhere(store, number=1):
    """Hold the issue's worktree lock from another thread until the returned event is set."""
    import threading

    taken, release = threading.Event(), threading.Event()

    def hold():
        with store.worktree_lock(number):
            taken.set()
            release.wait(5)

    thread = threading.Thread(target=hold)
    thread.start()
    assert taken.wait(5)
    return release, thread


def test_worktree_lock_timeout_tries_once_or_waits_then_reports_busy(store):
    import time

    from foragent.state_locks import LockBusy

    release, thread = _held_elsewhere(store)
    with pytest.raises(LockBusy):
        with store.worktree_lock(1, timeout=0):
            pass
    started = time.monotonic()
    with pytest.raises(LockBusy):
        with store.worktree_lock(1, timeout=0.3):
            pass
    assert time.monotonic() - started >= 0.3
    release.set()
    thread.join(5)
    with store.worktree_lock(1, timeout=0):  # Free again: taken at once.
        pass


def test_a_busy_worktree_prints_busy_after_delivering_the_message(store, tmp_path, monkeypatch, capsys):
    session(store, "claude")
    terminals = Terminals(monkeypatch)
    monkeypatch.setattr(lane_talk_open, "TALK_LOCK_WAIT", 0.2)
    release, thread = _held_elsewhere(store)
    try:
        assert _cli(store, monkeypatch, tmp_path, "--message", "Keep labels.", executor="claude") == 1
    finally:
        release.set()
        thread.join(5)
    out = capsys.readouterr()
    assert "next attempt" in out.out and "busy" in out.err
    assert (store.root / "hints/1.txt").read_text() == "Keep labels."  # Saved before the lock.
    assert terminals.opened() == [] and events(store, "talk-opened") == []


def test_a_timed_stop_skips_this_tick_while_the_worktree_is_busy(store, monkeypatch):
    from types import SimpleNamespace

    from foragent import scheduler_recovery

    killed, report = [], SimpleNamespace(actions=[])
    monkeypatch.setattr(scheduler_recovery, "_stop_run_processes",
                        lambda ctx, run, report, **kw: ctx.kill_runner(4242) or "refused")
    ctx = SimpleNamespace(store=store, config=config(), kill_runner=killed.append)
    release, thread = _held_elsewhere(store)
    try:
        assert scheduler_recovery._kill_run(ctx, 1, report, count_stall=True, unless_talking=True) is False
    finally:
        release.set()
        thread.join(5)
    assert killed == [] and report.actions == ["#1 stop skipped this tick: its worktree is busy"]
    assert store.get_run(1).restarts == 0


def test_the_tick_holds_the_worktree_lock_only_up_to_the_runner_signal(store, monkeypatch):
    from types import SimpleNamespace

    from foragent import scheduler_recovery
    from foragent.state_locks import LockBusy

    seen = []

    def busy():
        try:
            with store.worktree_lock(1, timeout=0):
                return False
        except LockBusy:
            return True

    def kill_runner(pid):
        import threading

        result = []
        thread = threading.Thread(target=lambda: result.append(busy()))
        thread.start()
        thread.join(5)
        seen.append(("during signal", result[0]))

    def stop(ctx, run, report, **kw):
        ctx.kill_runner(4242)
        seen.append(("after signal", busy()))  # Before the executor stop and the worktree cleanup.
        return "refused"

    monkeypatch.setattr(scheduler_recovery, "_stop_run_processes", stop)
    ctx = SimpleNamespace(store=store, config=config(), kill_runner=kill_runner)
    scheduler_recovery._kill_run(ctx, 1, SimpleNamespace(actions=[]), unless_talking=True)
    assert seen == [("during signal", True), ("after signal", False)]


def test_only_live_events_make_a_talk_or_a_pause(store):
    append_event(store, NOW, 1, "talk-opened", "Talk opened for #1 (codex)",
                 run_started_at=store.get_run(1).started_at, executor="codex", mode="resume", handle="term_r")
    run = store.get_run(1)
    assert run.talking is False and run.talk_pause_seconds(NOW + timedelta(hours=1)) == 0
    talk_event(store, NOW)  # No mode field: live.
    run = store.get_run(1)
    assert run.talking is True and run.talk_pause_seconds(NOW + timedelta(minutes=5)) == 300
    assert [event.get("mode", "live") for event in store.resume_events(run)] == ["resume"]


def test_a_live_event_written_as_the_run_finished_never_becomes_its_resume_terminal(store, tmp_path, monkeypatch):
    session(store, "codex")
    terminals = Terminals(monkeypatch)

    def finish_during_launch(argv):
        if argv[1:3] == ["terminal", "create"] and store.get_run(1).status == "running":
            store.finish_run(1, "blocked")  # The run ends while its live log opens.

    terminals.on_call = finish_during_launch
    lanes.talk(1, store=store, config=config(), repo_root=tmp_path)
    terminals.on_call = None
    assert events(store, "talk-opened")[0]["mode"] == "live" and store.get_run(1).talking is False
    lanes.talk(1, store=store, config=config(), repo_root=tmp_path)  # Finished: a resume terminal.
    create = terminals.opened()[-1]
    assert len(terminals.opened()) == 2 and "resume thread-1" in create[create.index("--command") + 1]
    assert events(store, "talk-opened")[-1]["mode"] == "resume"


def test_a_stall_stop_skipped_for_a_talk_writes_no_stall_mark(store, monkeypatch):
    from types import SimpleNamespace

    from foragent import scheduler_recovery

    writes, signals = [], []
    original = store.update_run
    monkeypatch.setattr(store, "update_run", lambda *a, **kw: writes.append(kw) or original(*a, **kw))
    monkeypatch.setattr(scheduler_recovery, "_stop_run_processes",
                        lambda ctx, run, report, **kw: ctx.kill_runner(4242) or "stopped")
    ctx = SimpleNamespace(store=store, config=config(), kill_runner=signals.append)
    talk_event(store)  # Opened after the tick's first read: the re-read under the lock sees it.
    assert scheduler_recovery._kill_run(ctx, 1, SimpleNamespace(actions=[]), outcome="stalled-restart",
                                        count_stall=True, unless_talking=True) is False
    assert writes == [] and signals == []
    store.finish_run(1, "merged")  # The runner then finishes on its own: no false stall.
    assert store.get_run(1).outcome == "merged" and store.get_run(1).restarts == 0


def test_the_stall_mark_is_written_under_the_lock_right_before_the_signal(store, monkeypatch):
    from types import SimpleNamespace

    from foragent import scheduler_recovery

    order = []
    original = store.update_run
    monkeypatch.setattr(store, "update_run", lambda *a, **kw: order.append(("mark", kw.get("outcome")))
                        or original(*a, **kw))
    monkeypatch.setattr(scheduler_recovery, "_stop_run_processes",
                        lambda ctx, run, report, **kw: ctx.kill_runner(4242) or "refused")
    ctx = SimpleNamespace(store=store, config=config(), kill_runner=lambda pid: order.append(("signal", pid)))
    scheduler_recovery._kill_run(ctx, 1, SimpleNamespace(actions=[]), outcome="stalled-restart",
                                 count_stall=True, unless_talking=True)
    assert order[:2] == [("mark", "stalled-restart"), ("signal", 4242)]
    assert store.get_run(1).restarts == 0  # The refused stop was undone.


def test_the_runner_records_the_live_session_through_its_executor_loop(run_case):
    from dataclasses import replace

    from foragent.executors import ExecResult
    from foragent.run_execution import _available_executor

    c = run_case
    polls, seen = [], []

    def hook(execute, poll):
        polls.append(poll)
        return execute()

    ctx = replace(c.ctx, config=replace(c.ctx.config, executor="codex"), poll_hook=hook)
    log = c.store.log_path(1)

    class Executor:
        def run(self, **kwargs):
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("a") as stream:
                stream.write(json.dumps({"type": "thread.started", "thread_id": "live-thread"}) + "\n")
            polls[-1]()  # The 30-second poll fires while the worker is still running.
            seen.append(c.store.get_run(1).session_id)
            return ExecResult(0, False, "done", kwargs["log_path"], num_turns=1)

    _available_executor(1, ctx, Executor(), session_body="body", resume_prompt=None,
                        worktree=ctx.repo_root, prompt="full task", model=None, log_path=log)
    assert seen == ["live-thread"]
    c.store.finish_run(1, "merged")
    c.tracker.remove_labels(1, ["running"])


def _finished_run_pause(tmp_path, *opened, closed=None, finish_after=300):
    """A run that started at NOW and finished ``finish_after`` seconds later, with given live talks."""
    clock = [NOW]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    store.start_run(1, branch="foragent/issue-1", worktree=store.worktree_path(1))
    events = [talk_event(store, NOW + timedelta(seconds=at), handle=f"term_{at}") for at in opened]
    if closed is not None:
        event, at = events[closed[0]], closed[1]
        append_event(store, NOW + timedelta(seconds=at), 1, "talk-closed", "Talk closed for #1",
                     run_started_at=event["run_started_at"], executor="codex", handle=event["handle"])
    clock[0] = NOW + timedelta(seconds=finish_after)
    store.finish_run(1, "merged")
    return store.get_run(1).talk_pause_seconds(NOW + timedelta(hours=1))


def test_a_talk_superseded_after_the_run_finished_counts_only_up_to_the_finish(tmp_path):
    # Talk A from 0; the run finishes at 300; a replacement opened at 310 (during a slow launch).
    assert _finished_run_pause(tmp_path, 0, 310) == 300


def test_a_talk_closed_after_the_run_finished_counts_only_up_to_the_finish(tmp_path):
    assert _finished_run_pause(tmp_path, 0, closed=(0, 400)) == 300


def test_a_talk_opened_after_the_run_finished_adds_nothing(tmp_path):
    assert _finished_run_pause(tmp_path, 100, 350, closed=(0, 150)) == 50


def test_a_running_runs_talk_never_counts_before_its_start(store):
    talk_event(store, NOW - timedelta(seconds=60))  # A clock that ran ahead of the run's start.
    assert store.get_run(1).talk_pause_seconds(NOW + timedelta(seconds=30)) == 30


def test_the_ticks_talk_step_keeps_an_open_talk_and_reports_a_closed_one(store, tmp_path, monkeypatch):
    from types import SimpleNamespace

    session(store, "codex")
    terminals = Terminals(monkeypatch)
    lanes.talk(1, store=store, config=config(), repo_root=tmp_path)
    ctx = SimpleNamespace(store=store, repo_root=tmp_path, now=lambda: NOW, config=config())
    report = SimpleNamespace(actions=[])
    assert lane_talk.tick_talk(ctx, store.get_run(1), report) is True and report.actions == []
    terminals.alive = False
    assert lane_talk.tick_talk(ctx, store.get_run(1), report) is False
    assert report.actions == ["#1 talk terminal closed; stall clock restarted"]


@pytest.mark.parametrize("limit", ["runtime", "stall"])
def test_the_ticks_timed_stops_go_through_the_talk_guard(limit, git_history, tmp_path, monkeypatch, fake_process_table):
    import test_scheduler as sched

    from foragent import scheduler
    from foragent.scheduler import tick

    ctx = sched.ctx.__wrapped__(git_history, tmp_path, monkeypatch, fake_process_table)
    sched.approve(ctx)
    tick(ctx)
    sched.mark_running(ctx)
    minutes = ctx.config.max_runtime_minutes + 11 if limit == "runtime" else ctx.config.stall_minutes + 1
    ctx.now = lambda: sched.NOW + timedelta(minutes=minutes)
    if limit == "runtime":
        ctx.store.update_run(1, last_progress_at=ctx.now().isoformat())  # Busy, but past the runtime limit.
    guarded = []
    original = scheduler.lane_talk.guarded_stop
    monkeypatch.setattr(scheduler.lane_talk, "guarded_stop",
                        lambda ctx, number, report, stop, mark=None: guarded.append(number)
                        or original(ctx, number, report, stop, mark))
    tick(ctx)
    assert guarded == [1] and ctx.store.get_run(1).status == "finished"
