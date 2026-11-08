"""r1144 follow-up of #2656: a reviewer's retry wait does not keep `fa review` alive after a stop.

Statement: when a reviewer is in a retry wait as the stop begins, fa review exits within the
prompt-exit bound.

Two reviewers run at once (`review_reviewers: 2`). The first answers like an executor that met
HTTP 503 (a structured transient error), so fa puts it in its retry wait; the project's
`transient_max_wait_minutes: 1` makes that wait 60 seconds, far above the bound. While it
waits, the second reviewer's session receives SIGINT or SIGTERM through the handler fa
registered (a fake signal API, as in the locked test_review_stop_fails_closed.py). fa must
return a stopped exit, and every thread it started must be gone, within the same bound the
locked test_review_branch_empty_and_interrupt.py sets (EXIT_WITHIN_S): a live non-daemon
reviewer thread keeps the interpreter from exiting.

Nothing real runs: reviewer judgments are scripted at the public executor boundary and process
I/O is the shared fake process table; no OS signal is sent.

Break-it: a retry wait that sleeps its full time after the stop, a stop that ends the wait but
then starts a further reviewer session, and an fa that returns while the waiting thread still
runs each fail here.
"""

import signal
import threading
import time

import pytest
import yaml

from foragent import capacity
from foragent.executors import ClaudeExecutor, CodexExecutor, ExecResult
from test_review_branch import Script, fa, project  # noqa: F401
from test_review_branch_empty_and_interrupt import EXIT_WITHIN_S

SIGNALS = pytest.mark.parametrize(
    "signum", [signal.SIGINT, signal.SIGTERM], ids=["SIGINT", "SIGTERM"],
)
UNAVAILABLE = "API Error: 503 Service Unavailable"
RETRY_WAIT_S = 60  # transient_max_wait_minutes: 1


@pytest.fixture
def waiting_review(project, fake_process_table, monkeypatch, capsys):
    settings = project / ".foragent.yml"
    config = yaml.safe_load(settings.read_text(encoding="utf-8"))
    config.update(review_reviewers=2, transient_max_wait_minutes=1)
    settings.write_text(yaml.safe_dump(config), encoding="utf-8")
    # Admission must not wait on this machine's real load; the panel needs two slots at once.
    monkeypatch.setattr(capacity, "_sensor_block", lambda: None)

    handlers = {}

    def register(number, handler):
        previous = handlers.get(number, signal.SIG_DFL)
        handlers[number] = handler
        return previous

    monkeypatch.setattr(signal, "signal", register)
    monkeypatch.setattr(signal, "getsignal", lambda number: handlers.get(number, signal.SIG_DFL))

    def run(signum):
        script = Script(review="VERDICT: PASS")
        lock = threading.Lock()
        sessions, after_stop, stopped_at = [], [], []
        waiting = threading.Event()  # the first reviewer has its 503 answer and enters the wait

        def interrupt():
            stopped_at.append(time.monotonic())
            handler = handlers.get(signum, signal.SIG_DFL)
            if callable(handler):
                handler(signum, None)
            elif handler == signal.SIG_DFL:
                # Model the default action without asking the OS to send it.
                if signum == signal.SIGINT:
                    raise KeyboardInterrupt
                raise SystemExit(128 + signum)

        def review(**kwargs):
            prompt = kwargs["prompt"]
            scoring = prompt.lstrip().startswith("{") and '"finding"' in prompt
            with lock:
                if stopped_at:
                    after_stop.append(prompt[:80])
                index = None if scoring else len(sessions)
                if index is not None:
                    sessions.append(index)
            if index == 0:
                waiting.set()
                return ExecResult(1, False, UNAVAILABLE, kwargs["log_path"], UNAVAILABLE,
                                  log_tail=UNAVAILABLE, error_kind="transient")
            if index == 1:
                assert waiting.wait(30), "The first reviewer got its 503 answer"
                threading.Event().wait(1.0)  # let it settle into its retry wait
                interrupt()
            return script.run(**kwargs)

        monkeypatch.setattr(CodexExecutor, "run", staticmethod(review))
        monkeypatch.setattr(ClaudeExecutor, "run", staticmethod(review))
        before = set(threading.enumerate())
        returned_at = None
        try:
            try:
                code, out, err = fa(["review", "feature", "--base", "main"], capsys)
            except KeyboardInterrupt:
                captured = capsys.readouterr()
                code, out, err = None, captured.out, captured.err
            returned_at = time.monotonic()
            assert stopped_at, f"The second reviewer's session received the signal\n{out}{err}"
            deadline = stopped_at[0] + EXIT_WITHIN_S
            started = [thread for thread in threading.enumerate()
                       if thread not in before and not thread.daemon]
            for thread in started:
                thread.join(max(0.0, deadline - time.monotonic()))
            lingering = [thread.name for thread in started if thread.is_alive()]
            exited = max(returned_at, time.monotonic()) - stopped_at[0]
        finally:
            # Never leave a reviewer thread running past the fakes' teardown.
            for thread in set(threading.enumerate()) - before:
                if not thread.daemon:
                    thread.join(RETRY_WAIT_S * 4)
        return code, out + err, exited, lingering, after_stop

    return run


@SIGNALS
def test_a_reviewer_in_its_retry_wait_lets_fa_exit_within_the_prompt_exit_bound(waiting_review, signum):
    code, output, exited, lingering, after_stop = waiting_review(signum)

    assert exited < EXIT_WITHIN_S and not lingering, (
        f"fa exits within {EXIT_WITHIN_S}s of the signal although a reviewer was in its "
        f"{RETRY_WAIT_S}s retry wait ({exited:.1f}s, threads still running: {lingering})\n{output}")
    assert code not in (None, 0, 1), f"A stopped review is neither PASS nor FAIL: {code}\n{output}"
    assert "stop" in output.lower() and "Traceback" not in output and "KeyboardInterrupt" not in output, \
        f"One plain stop message, no traceback:\n{output}"
    assert after_stop == [], f"The cancelled wait starts no further reviewer session: {after_stop}"
