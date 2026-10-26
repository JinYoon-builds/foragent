"""Acceptance test for single-lane-view. Locked by Foragent; do not edit."""

from datetime import timedelta
import fcntl
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from test_local_truth import record_gh
from test_skills_autosync import cli_exit, isolated_home
from test_view_open_triggers import foreground_argv, foreground_case


@pytest.fixture
def lane_view(git_history, tmp_path, monkeypatch, isolated_home):
    """Use the established offline CLI, with no desktop or curses execution."""
    import curses

    from foragent import cli, watch, watch_open

    case, main, commands = foreground_case(git_history, tmp_path, monkeypatch)
    gh_calls = record_gh(monkeypatch)
    state = SimpleNamespace(case=case, main=main, calls=[], timeout=False, live_pids={os.getpid()})

    def call(argv, root):
        state.calls.append((list(argv), root))
        if state.timeout:
            state.timeout = False
            raise subprocess.TimeoutExpired(argv, 5)

    def probe(pid, sig):
        assert sig == 0, "Acceptance tests must never deliver process signals"
        if pid not in state.live_pids:
            raise ProcessLookupError(pid)

    def forbidden(*args, **kwargs):
        pytest.fail("A real TUI loop or process replacement must never start")

    monkeypatch.setattr(watch_open, "_call", call)
    monkeypatch.setattr(watch_open, "datetime", cli.datetime)
    monkeypatch.setattr(os, "kill", probe)
    monkeypatch.setattr(os, "execv", forbidden)
    monkeypatch.setattr(curses, "wrapper", forbidden)
    monkeypatch.setattr(watch, "collect", lambda *args, **kwargs: object())
    yield state
    assert gh_calls == [], "All GitHub operations must use the offline tracker"
    assert commands.calls() == [], "Only the patched _call may record terminal commands"


def locked_files(root):
    """Probe independent descriptors: the contract leaves the lock filename open."""
    held = set()
    for path in root.iterdir():
        if path.is_file():
            with path.open("rb") as stream:
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    held.add(path)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    return held


def watch_argv(case, *, opened=False):
    return ["gate", "watch", "--repo-root", str(case.root), *(["--open"] if opened else [])]


def run_view(lane_view, monkeypatch):
    # pytest replaces captured streams between setup and the test call phase.
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    return cli_exit(lane_view.main, watch_argv(lane_view.case))


def test_single_lane_view_timeout_spaces_publish_orch_and_gate_reopen(lane_view, capsys):
    from foragent import watch_open
    from foragent.state import StateStore

    case, main = lane_view.case, lane_view.main
    attempted_at = case.clock[0]
    lane_view.timeout = True
    assert cli_exit(main, watch_argv(case, opened=True)) == 0
    assert len(lane_view.calls) == 1
    assert "terminal opener timed out" in capsys.readouterr().err
    assert not (case.store.root / "watch.pid").exists(), "The delayed view has not registered yet"

    case.clock[0] += timedelta(seconds=1)
    assert cli_exit(main, foreground_argv(case, "publish")) == 0
    case.clock[0] += timedelta(seconds=1)
    assert cli_exit(main, foreground_argv(case, "orch")) == 0
    case.clock[0] += timedelta(seconds=1)
    # A fresh store models a different gate process reading the shared record.
    watch_open.reopen(case.root, StateStore(case.store.root), "en", case.clock[0])

    assert len(lane_view.calls) == 1, (
        "A timed-out direct opener must reserve the spacing for publish, orch watch, "
        f"and gate reopen too; terminal commands: {lane_view.calls}"
    )
    record = json.loads((case.store.root / "watch-open.json").read_text())
    assert watch_open.datetime.fromisoformat(record["attempted_at"]) == attempted_at


def test_single_lane_view_lock_suppresses_every_opener_without_a_pid(lane_view, monkeypatch):
    import curses

    from foragent import watch_open
    from foragent.state import StateStore

    case, main = lane_view.case, lane_view.main
    observed = {}

    def terminal(*args, **kwargs):
        observed["held_before"] = locked_files(case.store.root)
        pid_file = case.store.root / "watch.pid"
        original_pid = pid_file.read_bytes()
        # Exercise the slow-start window: a held lock must suffice before PID publication.
        pid_file.unlink()
        try:
            openers = [
                ("open_view", lambda: watch_open.open_view(case.root, StateStore(case.store.root))),
                ("gate watch --open", lambda: cli_exit(main, watch_argv(case, opened=True))),
                *[(command, lambda command=command: cli_exit(main, foreground_argv(case, command)))
                  for command in ("publish", "orch", "init", "lanes")],
                ("gate reopen", lambda: watch_open.reopen(case.root, case.store, "en", case.clock[0])),
            ]
            observed["commands_by_entry"] = {}
            for name, opener in openers:
                # An old attempt record cannot hide a missing live-lock check.
                case.clock[0] += watch_open.REOPEN_SPACING + timedelta(seconds=1)
                before = len(lane_view.calls)
                assert opener() in (None, 0)
                observed["commands_by_entry"][name] = len(lane_view.calls) - before
            observed["held_after"] = locked_files(case.store.root)
        finally:
            pid_file.write_bytes(original_pid)
        return False

    monkeypatch.setattr(curses, "wrapper", terminal)
    assert run_view(lane_view, monkeypatch) == 0
    assert observed["commands_by_entry"] == dict.fromkeys(observed["commands_by_entry"], 0), (
        "A live view's kernel lock must suppress every opener even without watch.pid: "
        f"{observed['commands_by_entry']}"
    )
    assert observed["held_before"], "The running view must hold an exclusive flock in its state root"
    assert observed["held_after"] == observed["held_before"], "Hold the same lock for the whole view"
    assert locked_files(case.store.root) == set(), "Returning from the view must release the lock"


def test_single_lane_view_second_start_exits_zero_without_registering_pid(
    lane_view, monkeypatch, capsys,
):
    import curses

    case, main = lane_view.case, lane_view.main
    pid_file = case.store.root / "watch.pid"
    first_pid, second_pid = 71001, 71002
    lane_view.live_pids.update((first_pid, second_pid))
    pid_writes, terminal_pids = [], []
    observed = {}
    atomic_write = case.store._atomic_write

    def record_write(path, content):
        if path == pid_file:
            pid_writes.append(int(content))
        return atomic_write(path, content)

    def terminal(*args, **kwargs):
        terminal_pids.append(os.getpid())
        if len(terminal_pids) > 1:
            return False  # Bound today's duplicate view; never run its TUI.
        observed["held_before"] = locked_files(case.store.root)
        assert int(pid_file.read_text()) == first_pid
        capsys.readouterr()
        # Independent opens in one process contend on flock just like two processes.
        with monkeypatch.context() as second:
            second.setattr(os, "getpid", lambda: second_pid)
            observed["second_status"] = run_view(lane_view, second)
        captured = capsys.readouterr()
        observed["second_lines"] = (captured.out + captured.err).strip().splitlines()
        observed["pid_after_second"] = pid_file.read_text() if pid_file.exists() else None
        observed["held_after"] = locked_files(case.store.root)
        return False

    monkeypatch.setattr(case.store, "_atomic_write", record_write)
    monkeypatch.setattr(os, "getpid", lambda: first_pid)
    monkeypatch.setattr(curses, "wrapper", terminal)
    assert run_view(lane_view, monkeypatch) == 0
    assert observed["second_status"] == 0, "A duplicate view must exit successfully"
    assert terminal_pids == [first_pid], (
        "Only the first view may enter curses; the competing view must exit immediately: "
        f"{terminal_pids}"
    )
    assert pid_writes == [first_pid], "The second view must never register watch.pid, even temporarily"
    assert int(observed["pid_after_second"]) == first_pid, "The first view must keep its PID registration"
    assert len(observed["second_lines"]) == 1, "A duplicate must explain its exit on exactly one line"
    line = observed["second_lines"][0].lower()
    assert all(word in line for word in ("view", "already", "open")), line
    assert observed["held_before"] and observed["held_after"] == observed["held_before"]
    assert not pid_file.exists(), "Only the first view's eventual exit removes its PID"
    assert locked_files(case.store.root) == set()
    assert lane_view.calls == []


def test_single_lane_view_exit_and_elapsed_spacing_allow_one_more_open(lane_view, monkeypatch):
    import curses

    from foragent import watch_open

    case, main = lane_view.case, lane_view.main
    assert cli_exit(main, watch_argv(case, opened=True)) == 0
    assert len(lane_view.calls) == 1
    observed = {}

    def terminal(*args, **kwargs):
        observed["held"] = locked_files(case.store.root)
        case.clock[0] += watch_open.REOPEN_SPACING + timedelta(seconds=1)
        watch_open.reopen(case.root, case.store, "en", case.clock[0])
        assert len(lane_view.calls) == 1, "Even elapsed spacing cannot reopen a live view"
        return False

    monkeypatch.setattr(curses, "wrapper", terminal)
    assert run_view(lane_view, monkeypatch) == 0
    assert not (case.store.root / "watch.pid").exists()
    assert locked_files(case.store.root) == set(), "The exited view must leave no held kernel lock"

    assert cli_exit(main, foreground_argv(case, "orch")) == 0
    assert len(lane_view.calls) == 2, (
        "After the holder exits and spacing passes, the next opener must start "
        f"exactly one new terminal command; recorded {len(lane_view.calls)}"
    )
    # Repeated recoveries have no count cap; only spacing and a live lock matter.
    for expected in range(3, 11):
        case.clock[0] += watch_open.REOPEN_SPACING + timedelta(seconds=1)
        watch_open.reopen(case.root, case.store, "en", case.clock[0])
        assert cli_exit(main, watch_argv(case, opened=True)) == 0
        assert len(lane_view.calls) == expected, "Each elapsed spacing permits one attempt without a count cap"
    assert observed["held"], "Recovery must follow a view that actually held an exclusive kernel lock"
