"""PM q36 / #2215: an interrupted review signals only processes it can verify.

Reuse the review CLI's offline Git fixtures and the shared fake process table.
Call the handler registered through a fake signal API. Fail-closed cases let the
reviewer leader finish before the pending interrupt, leaving its observed helpers
running; this also lets a replacement reuse the reaped leader's PID realistically.
Positive cleanup cases can interrupt while the leader is still running instead.
"""

import io
import signal
import subprocess

import pytest

from foragent import processes
from foragent.executors import ClaudeExecutor, CodexExecutor
from test_review_branch import Script, fa, project  # noqa: F401


REVIEWER, ANCESTOR, CHILD = 42000, 42001, 42002
SIGNALS = pytest.mark.parametrize(
    "signum", [signal.SIGINT, signal.SIGTERM], ids=["SIGINT", "SIGTERM"],
)


@pytest.fixture
def interrupted_review(project, fake_process_table, monkeypatch, capsys):
    """Run the public CLI with fake reviewer process I/O and signal registration."""
    table = fake_process_table
    handlers = {}
    started = []
    pending_interrupt = None
    script = Script(review="VERDICT: PASS")

    def register(number, handler):
        previous = handlers.get(number, signal.SIG_DFL)
        handlers[number] = handler
        return previous

    # Both review and run_logged see this fake API; no OS handler is installed.
    monkeypatch.setattr(signal, "signal", register)
    monkeypatch.setattr(signal, "getsignal", lambda number: handlers.get(number, signal.SIG_DFL))

    class ReviewerProcess:
        pid = REVIEWER
        returncode = None

        def __init__(self, argv, **kwargs):
            self.stdin = io.BytesIO()
            for pid, parent, birth in (
                (REVIEWER, table.self_pid, 10), (ANCESTOR, REVIEWER, 11),
                (CHILD, ANCESTOR, 12),
            ):
                table.add(pid, ppid=parent, pgid=REVIEWER, sid=REVIEWER,
                          cwd=kwargs["cwd"], start_time=birth, argv=("fake-reviewer",))
            started.append(self.pid)

        def poll(self):
            return self.returncode

        def wait(self, **kwargs):
            if pending_interrupt is not None:
                pending_interrupt()
            # Only the leader exits. Its helpers were present in the launch
            # snapshot and remain live when the executor processes the result.
            table.exit(self.pid)
            self.returncode = 0
            return self.returncode

    popen = subprocess.Popen

    def launch(argv, *args, **kwargs):
        if argv == ["fake-reviewer"]:
            return ReviewerProcess(argv, **kwargs)
        assert isinstance(argv, (list, tuple)) and argv[0] == "git", \
            f"Only offline fixture Git may start a real process: {argv!r}"
        assert not kwargs.get("shell")
        return popen(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", launch)

    def run(signum, fault, *, while_running=False):
        nonlocal pending_interrupt
        interrupted = False

        def interrupt():
            nonlocal interrupted
            if interrupted:
                return
            interrupted = True
            fault(table)
            handler = handlers.get(signum, signal.SIG_DFL)
            if callable(handler):
                handler(signum, None)
            elif handler == signal.SIG_DFL:
                # Model the default action without asking the OS to send it.
                if signum == signal.SIGINT:
                    raise KeyboardInterrupt
                raise SystemExit(128 + signum)

        pending_interrupt = interrupt if while_running else None

        def review(**kwargs):
            if not interrupted:
                processes.run_logged(
                    ["fake-reviewer"], cwd=kwargs["worktree"], timeout_s=1,
                    log_path=kwargs["log_path"], prompt=kwargs["prompt"],
                )
                interrupt()
            return script.run(**kwargs)

        monkeypatch.setattr(CodexExecutor, "run", staticmethod(review))
        monkeypatch.setattr(ClaudeExecutor, "run", staticmethod(review))
        try:
            code, out, err = fa(["review", "feature", "--base", "main"], capsys)
        except KeyboardInterrupt:
            captured = capsys.readouterr()
            code, out, err = None, captured.out, captured.err
        assert started == [REVIEWER], "The CLI must actually start the fake reviewer session"
        return code, out + err

    return table, run


def assert_stopped(code, output):
    assert code not in (None, 0, 1), f"An interrupted review is neither PASS nor FAIL: {code}\n{output}"
    assert "stop" in output.lower(), f"The CLI must say the review stopped:\n{output}"
    assert "Traceback" not in output and "KeyboardInterrupt" not in output


@SIGNALS
@pytest.mark.parametrize("unreadable", ["permission", "missing"])
def test_unreadable_ancestor_row_gets_no_signal_and_a_reason(
        interrupted_review, monkeypatch, signum, unreadable):
    table, run = interrupted_review

    def fault(table):
        def listing():
            rows = [row for row in table.list_processes() if row.pid != ANCESTOR]
            return processes.ProcessTable(rows, unreadable=(ANCESTOR,) if unreadable == "permission" else ())

        def identity(pid):
            if pid == ANCESTOR and unreadable == "permission":
                raise PermissionError(f"ancestor {ANCESTOR} unreadable")
            return next((row for row in listing() if row.pid == pid), None)

        monkeypatch.setattr(processes, "list_processes", listing)
        monkeypatch.setattr(processes, "read_process_identity", identity)

    code, output = run(signum, fault)

    # A group signal could hit the child even if its own PID is never named.
    assert not table.signals, f"Unverifiable helpers must receive NO signal: {table.signals}\n{output}"
    assert CHILD in table.live, "The helper with an unreadable ancestor must be left running"
    assert_stopped(code, output)
    reason = output.lower()
    assert str(CHILD) in reason and any(word in reason for word in ("ancestor", "ancestry", "unreadable", "missing")), \
        f"Name the process left behind and why its ancestry cannot be verified:\n{output}"
    assert any(word in reason for word in ("left", "skipped", "not stopped", "could not", "cannot")), output


@SIGNALS
def test_failed_scan_sends_no_signal(interrupted_review, monkeypatch, signum):
    table, run = interrupted_review
    reason = "process scan unavailable: ownership cannot be verified"

    def fault(table):
        def failed_scan():
            raise PermissionError(reason)

        monkeypatch.setattr(processes, "list_processes", failed_scan)

    code, output = run(signum, fault)

    # No stop-stage verification preceded this failure, so the allowed set is empty.
    assert table.signals == [], f"A failed scan authorizes no signals: {table.signals}\n{output}"
    assert {ANCESTOR, CHILD} <= table.live
    assert_stopped(code, output)
    assert reason in output, f"Record why the processes could not be stopped:\n{output}"


@SIGNALS
def test_reused_pid_outside_the_session_is_never_signalled(interrupted_review, signum):
    table, run = interrupted_review

    def fault(table):
        assert REVIEWER not in table.live, "The recorded leader was reaped before PID reuse"
        table.add(REVIEWER, ppid=1, pgid=REVIEWER, sid=REVIEWER,
                  cwd=table.rows[table.self_pid].cwd, start_time=100,
                  argv=("unrelated-work",))

    code, output = run(signum, fault)

    assert all(abs(target) != REVIEWER for kind, target, number in table.signals), \
        f"Neither a PID nor a group signal may target the replacement: {table.signals}\n{output}"
    assert REVIEWER in table.live and table.rows[REVIEWER].start_time == 100
    assert_stopped(code, output)
