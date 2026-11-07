"""r1144 follow-up of #2656: `fa review`'s stop reaches verified descendants that appear late.

Statement: when a verified helper spawns a child while fa review is stopping, the stop also
signals that child, and fa does not report a clean stop while the child still runs.

Two late arrivals are modelled, both through the public CLI (`fa review feature`):

1. An observed helper whose leader already exited handles SIGTERM by spawning a child. The
   helper ignores SIGTERM, so the child's parent is still the verified helper when the stop
   scans again before SIGKILL. The child must be signalled too, by PID or by its own group.
2. A reviewer whose launch is still being registered (the process exists, ``Popen`` has not
   returned) when the stop starts. Its tree must still be stopped once the launch completes.

Process I/O is the shared fake process table (tests/process_fakes.py); signals are recorded in
memory and never reach the OS. Signal handlers are registered through a fake signal API, as in
the locked tests/acceptance/test_review_stop_fails_closed.py, whose fixture this file reuses.
#2656's fail-closed rule stays: hosts and unrelated processes never receive a signal.

Break-it: a SIGKILL stage that only re-signals the processes it sent SIGTERM (the late child is
found but left running unnamed), a stop that ignores a launch registered after it began, and a
stop that reports success while a late child it could not signal still runs each fail here.
"""

import errno
import io
import signal
import subprocess
import threading
import time

import pytest

from foragent import processes
from foragent.executors import ClaudeExecutor, CodexExecutor
from test_review_branch import Script, fa, project  # noqa: F401
from test_review_stop_fails_closed import (  # noqa: F401
    ANCESTOR, CHILD, REVIEWER, assert_stopped, interrupted_review,
)

LATE = 42003  # the child a helper spawns while the stop runs
SIGNALS = pytest.mark.parametrize(
    "signum", [signal.SIGINT, signal.SIGTERM], ids=["SIGINT", "SIGTERM"],
)
LEFT_WORDS = ("left", "skipped", "not stopped", "could not", "cannot", "still running", "running")


def hit(table, calls):
    """Every PID a recorded signal call reached or aimed at (group calls by row membership)."""
    pids = set()
    for kind, target, number in calls:
        if kind == "pid":
            pids.add(target)
        else:
            pids.update(pid for pid, row in table.rows.items() if row.pgid == target)
    return pids


def spawn_on_sigterm(table, *, group, spawned):
    """The helper handles SIGTERM by starting a child, and keeps running until SIGKILL."""
    table.ignore_term.add(ANCESTOR)

    def after(call):
        kind, target, number = call
        if spawned or number != signal.SIGTERM or ANCESTOR not in table.live:
            return
        helper = table.rows[ANCESTOR]
        if not ((kind == "pid" and target == ANCESTOR) or (kind == "pgid" and target == helper.pgid)):
            return
        table.add(LATE, ppid=ANCESTOR, pgid=REVIEWER if group == "reviewer-group" else LATE,
                  sid=helper.sid, cwd=helper.cwd, start_time=30, argv=("fake-reviewer-helper",))
        spawned.append(len(table.signals))

    table.after_signal = after


def assert_hosts_untouched(table, unrelated=()):
    survivors = set(table.host_pids) | set(unrelated)
    assert survivors <= table.live, f"Hosts and unrelated processes keep running: {table.signals}"
    assert not survivors & hit(table, table.signals), \
        f"No signal may reach a process whose ancestry is not verified: {table.signals}"


@SIGNALS
@pytest.mark.parametrize("group", ["reviewer-group", "own-group"])
def test_a_child_spawned_by_a_helper_during_sigterm_is_stopped_too(
        interrupted_review, project, signum, group):
    table, run = interrupted_review
    unrelated = 43000
    table.add(unrelated, ppid=1, cwd=project, start_time=20, argv=("unrelated-work",))
    spawned = []

    def fault(table):
        assert REVIEWER not in table.live and {ANCESTOR, CHILD} <= table.live, \
            "The reviewer leader exited and left its observed helpers running"
        spawn_on_sigterm(table, group=group, spawned=spawned)

    code, output = run(signum, fault)

    assert spawned, ("The stop sends SIGTERM to the verified helper, whose SIGTERM handling "
                     f"spawns the late child: {table.signals}\n{output}")
    late_calls = [call for call in table.signals[spawned[0]:] if call[2]]
    assert LATE in hit(table, late_calls), \
        f"The child born during the stop is signalled too: {table.signals}\n{output}"
    assert LATE not in table.live, f"The late child does not keep running after the stop:\n{output}"
    assert not {ANCESTOR, CHILD} & table.live, f"The verified helpers are stopped: {table.signals}"
    assert_hosts_untouched(table, (unrelated,))
    assert_stopped(code, output)


@SIGNALS
def test_a_late_child_that_still_runs_is_named_and_the_stop_is_not_reported_clean(
        interrupted_review, signum):
    table, run = interrupted_review
    spawned, refused = [], []

    def fault(table):
        spawn_on_sigterm(table, group="own-group", spawned=spawned)
        real = table._signal

        def refusing(kind, target, number, members):
            if number and LATE in members:
                # The late child cannot be signalled (another user's setuid helper, say).
                table.calls.append((kind, target, number))
                table.signals.append((kind, target, number))
                refused.append((kind, target, number))
                raise PermissionError(errno.EPERM, "Operation not permitted")
            return real(kind, target, number, members)

        table._signal = refusing

    code, output = run(signum, fault)

    assert spawned, f"The stop sends SIGTERM to the verified helper: {table.signals}\n{output}"
    assert refused, f"The stop tries to signal the verified late child: {table.signals}\n{output}"
    assert LATE in table.live
    assert_stopped(code, output)
    text = output.lower()
    assert str(LATE) in output and any(word in text for word in LEFT_WORDS), \
        f"A stop that left a verified child running names it and says it is still running:\n{output}"
    assert_hosts_untouched(table)


@pytest.fixture
def registering_review(project, fake_process_table, monkeypatch, capsys):
    """The stop starts while the reviewer's launch is still inside ``Popen``.

    The fake ``Popen`` creates the reviewer tree, then waits until another thread has run the
    registered signal handler, and only then returns its handle to fa. The reviewer runs until
    it is signalled; without a stop it ends by itself after a few seconds with a PASS reply.
    """
    table = fake_process_table
    handlers, started = {}, []
    in_popen, released = threading.Event(), threading.Event()
    handler_errors = []
    script = Script(review="VERDICT: PASS")

    def register(number, handler):
        previous = handlers.get(number, signal.SIG_DFL)
        handlers[number] = handler
        return previous

    monkeypatch.setattr(signal, "signal", register)
    monkeypatch.setattr(signal, "getsignal", lambda number: handlers.get(number, signal.SIG_DFL))

    class ReviewerProcess:
        pid = REVIEWER
        returncode = None

        def __init__(self, argv, **kwargs):
            self.stdin = io.BytesIO()
            self.give_up = time.monotonic() + 3
            for pid, parent, birth in ((REVIEWER, table.self_pid, 10), (ANCESTOR, REVIEWER, 11),
                                       (CHILD, ANCESTOR, 12)):
                table.add(pid, ppid=parent, pgid=REVIEWER, sid=REVIEWER, cwd=kwargs["cwd"],
                          start_time=birth, argv=("fake-reviewer",))
            started.append(self.pid)
            in_popen.set()
            assert released.wait(10), "The signal handler ran while this launch was pending"

        def poll(self):
            if self.returncode is None and self.pid not in table.live:
                numbers = [n for kind, target, n in table.signals
                           if n and (target == self.pid or (kind == "pgid" and target == REVIEWER))]
                self.returncode = -(numbers[-1] if numbers else signal.SIGKILL)
            return self.returncode

        def wait(self, timeout=None):
            deadline = None if timeout is None else time.monotonic() + timeout
            while self.poll() is None:
                if time.monotonic() >= self.give_up:
                    table.exit(self.pid)  # no stop reached it: the reviewer finishes normally
                    self.returncode = 0
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired("fake-reviewer", timeout)
                threading.Event().wait(0.01)
            return self.returncode

        def terminate(self):
            table.kill(self.pid, signal.SIGTERM)

        def kill(self):
            table.kill(self.pid, signal.SIGKILL)

    popen = subprocess.Popen

    def launch(argv, *args, **kwargs):
        if argv == ["fake-reviewer"]:
            return ReviewerProcess(argv, **kwargs)
        assert isinstance(argv, (list, tuple)) and argv[0] == "git", \
            f"Only offline fixture Git may start a real process: {argv!r}"
        assert not kwargs.get("shell")
        return popen(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", launch)

    def run(signum):
        def deliver():
            if not in_popen.wait(30):
                return
            handler = handlers.get(signum, signal.SIG_DFL)
            try:
                if callable(handler):
                    handler(signum, None)
                # SIG_DFL: main installs no handler; nothing on this thread stops the reviewer.
            except BaseException as error:  # a handler may raise on the thread that runs it
                handler_errors.append(error)
            finally:
                released.set()

        def review(**kwargs):
            if not started:
                processes.run_logged(["fake-reviewer"], cwd=kwargs["worktree"], timeout_s=30,
                                     log_path=kwargs["log_path"], prompt=kwargs["prompt"])
            return script.run(**kwargs)

        monkeypatch.setattr(CodexExecutor, "run", staticmethod(review))
        monkeypatch.setattr(ClaudeExecutor, "run", staticmethod(review))
        signaller = threading.Thread(target=deliver, daemon=True)
        signaller.start()
        try:
            code, out, err = fa(["review", "feature", "--base", "main"], capsys)
        except (KeyboardInterrupt, SystemExit) as exc:
            captured = capsys.readouterr()
            code = exc.code if isinstance(exc, SystemExit) else None
            out, err = captured.out, captured.err
        finally:
            released.set()
            signaller.join(10)
        assert started == [REVIEWER], "The CLI must actually start the fake reviewer session"
        return code, out + err

    return table, run


@SIGNALS
def test_a_reviewer_whose_launch_is_still_registering_is_stopped(registering_review, project, signum):
    table, run = registering_review
    trees = {REVIEWER, ANCESTOR, CHILD}
    unrelated = 43000
    table.add(unrelated, ppid=1, cwd=project, start_time=20, argv=("unrelated-work",))

    code, output = run(signum)

    assert trees <= hit(table, table.signals) and not trees & table.live, \
        f"The reviewer tree registered after the stop began is stopped too: {table.signals}\n{output}"
    assert_stopped(code, output)
    assert_hosts_untouched(table, (unrelated,))
