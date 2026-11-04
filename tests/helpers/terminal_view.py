"""Offline terminal sessions and their view state for unit tests."""

from datetime import timedelta
import os
import re
import select
import time

import pytest

from helpers.home import isolated_home


def _status_case(git_history, tmp_path, monkeypatch, *, language="en"):
    from datetime import timedelta

    from foragent.tracker import Dependency
    from helpers.review_case import bind_cli, make_case

    lanes = {slug: {"paths": [f"{slug}/**"], "title": slug.title(), "emoji": "🧩"}
             for slug in ("api", "review", "decision", "web", "docs")}
    case = make_case(git_history, tmp_path, monkeypatch, lanes=lanes, language=language)
    case.tracker.add_labels(1, ["lane:api"])
    case.store.update_run(1, lane="api", phase="reviewing",
                          phase_started_at=(case.clock[0] - timedelta(seconds=120)).isoformat(),
                          last_activity_at=(case.clock[0] - timedelta(seconds=660)).isoformat())
    case.tracker.add_issue(2, "Review dispute", labels=["approved", "blocked", "lane:review"])
    case.tracker.add_issue(3, "Choose locale", labels=["approved", "needs-human", "lane:decision"])
    case.tracker.add_issue(4, "Login page", labels=["approved", "lane:web"])
    case.tracker.set_blocked_by(4, [Dependency(1, "open")])
    return case, bind_cli(case, monkeypatch)


FONT = "D2Coding"
_COLUMNS, _ROWS = 120, 30


@pytest.fixture(autouse=True)
def offline_terminal(isolated_home, tmp_path, monkeypatch):
    import socket

    from foragent import executors

    def forbidden(*args, **kwargs):
        raise AssertionError("the view test must not open a network connection or an executor")

    monkeypatch.setenv("FORAGENT_ORCH_SPAWN", "off")
    monkeypatch.setenv("FORAGENT_VIEW", "off")
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setenv("COLUMNS", str(_COLUMNS))
    monkeypatch.setenv("LINES", str(_ROWS))
    for name in ("LC_ALL", "LC_CTYPE", "TERM_PROGRAM", "TERM_PROGRAM_VERSION", "ORCA_TERMINAL_HANDLE",
                 "TMUX", "ITERM_SESSION_ID", "WEZTERM_PANE", "KITTY_WINDOW_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(executors.CodexExecutor, "run", forbidden)
    monkeypatch.setattr(executors.ClaudeExecutor, "run", forbidden)


def korean_view(git_history, tmp_path, monkeypatch, language="ko"):
    """A repository whose view has lane rows and issue titles, through the bound public CLI."""
    case, main = _status_case(git_history, tmp_path, monkeypatch, language=language)
    case.store.update_run(1, pid=os.getpid(), phase="implementing",
                          last_activity_at=(case.clock[0] - timedelta(seconds=30)).isoformat())
    case.tracker.add_issue(5, "진님 답할 질문 정리", labels=["approved", "lane:docs"])
    return case, main


def _screen_text(raw):
    """Every character the terminal would draw, with control sequences removed."""
    value = raw.decode("utf-8", errors="replace")
    value = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", value)       # OSC (links, titles)
    value = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)                 # CSI
    value = re.sub(r"\x1b[()*+][0-9A-Za-z]", "", value)                   # charset selection
    value = re.sub(r"\x1b[ -/]*[0-~]", "", value)                         # other ESC sequences
    return re.sub(r"[\x00-\x08\x0e-\x1f\x7f]", "", value)


def live_session(main, root, term_program):
    """Run `fa gate watch` in a 120x30 pty, wait for the first frame, press q, return all bytes."""
    import fcntl
    import pty
    import struct
    import termios

    pid, fd = pty.fork()
    if pid == 0:  # child: the real CLI entry point on a real terminal
        code = 1
        try:
            import sys
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", _ROWS, _COLUMNS, 0, 0))
            # pytest's capture replaced the Python streams; point them back at the terminal.
            sys.stdin = open(0, "r", encoding="utf-8", closefd=False)
            sys.stdout = open(1, "w", encoding="utf-8", closefd=False, buffering=1)
            sys.stderr = open(2, "w", encoding="utf-8", closefd=False, buffering=1)
            if term_program is None:
                os.environ.pop("TERM_PROGRAM", None)
            else:
                os.environ["TERM_PROGRAM"] = term_program
            code = main(["gate", "watch", "--repo-root", str(root)]) or 0
        except BaseException:  # noqa: BLE001  (the parent reports a non-zero exit)
            import traceback
            traceback.print_exc()
        finally:
            os._exit(code)
    chunks, deadline, quit_at = [], time.monotonic() + 30, None
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.2)
            if ready:
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    break
                if not data:
                    break
                chunks.append(data)
            entered = b"\x1b[?1049h" in b"".join(chunks)
            if entered and quit_at is None:
                quit_at = time.monotonic() + 3.0  # let the first frames and any hint paint
            if quit_at is not None and time.monotonic() >= quit_at:
                os.write(fd, b"q")
                quit_at = float("inf")
    finally:
        _, status = os.waitpid(pid, 0)
        os.close(fd)
    raw = b"".join(chunks)
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0, _screen_text(raw)[-2000:]
    assert b"\x1b[?1049h" in raw, "the live view must open on the alternate screen"
    return _screen_text(raw)
