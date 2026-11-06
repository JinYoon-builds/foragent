"""Acceptance test for terminal-app-hangul-hint. Locked by Foragent; do not edit.

macOS Terminal.app's default profile font (SF Mono) has no Hangul. Terminal.app
draws each syllable in the fallback font (Apple SD Gothic Neo) at about 70 % of
the two cells the view reserves for it, so Korean reads with a gap after every
syllable. The width maths of the view is right; only a font with Hangul glyphs
exactly two cells wide fixes the look. D2Coding is such a font (measured with
CoreText: Hangul 12.0 pt = 2 x 6.0 pt ASCII advance at 12 pt, box and block
glyphs one cell), so the hint names it.

These tests run the real `fa gate watch` live view in a pseudo-terminal and read
every byte the process writes to it. They do not decide where the hint appears
(inside the view, before it opens or after it closes) nor its wording.
"""

from datetime import timedelta
import os
import re
import select
import time

import pytest

from test_skills_autosync import isolated_home  # noqa: F401  (HOME isolation)

FONT = "D2Coding"
COLUMNS, ROWS = 120, 30


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
    monkeypatch.setenv("COLUMNS", str(COLUMNS))
    monkeypatch.setenv("LINES", str(ROWS))
    for name in ("LC_ALL", "LC_CTYPE", "TERM_PROGRAM", "TERM_PROGRAM_VERSION", "ORCA_TERMINAL_HANDLE",
                 "TMUX", "ITERM_SESSION_ID", "WEZTERM_PANE", "KITTY_WINDOW_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(executors.CodexExecutor, "run", forbidden)
    monkeypatch.setattr(executors.ClaudeExecutor, "run", forbidden)


def korean_view(git_history, tmp_path, monkeypatch, language="ko"):
    """A repository whose view has lane rows and issue titles, through the bound public CLI."""
    from test_view_status import status_case

    case, main = status_case(git_history, tmp_path, monkeypatch, language=language)
    case.store.update_run(1, pid=os.getpid(), phase="implementing",
                          last_activity_at=(case.clock[0] - timedelta(seconds=30)).isoformat())
    case.tracker.add_issue(5, "진님 답할 질문 정리", labels=["approved", "lane:docs"])
    # r53: the dashboard draws the snapshot the gate writes on its tick.
    from foragent.scheduler import tick
    from test_orch_review_handoff import tick_context
    tick(tick_context(case, start_runs=False))
    return case, main


def screen_text(raw):
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
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLUMNS, 0, 0))
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
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0, screen_text(raw)[-2000:]
    assert b"\x1b[?1049h" in raw, "the live view must open on the alternate screen"
    return screen_text(raw)


def test_first_terminal_app_run_in_korean_names_a_hangul_font_once(git_history, tmp_path, monkeypatch):
    case, main = korean_view(git_history, tmp_path, monkeypatch)
    first = live_session(main, case.root, "Apple_Terminal")
    assert "진님 답할 질문" in first, "the live view itself must still be drawn"
    assert FONT in first, "the first Korean view in Terminal.app names a font that draws Hangul without gaps"
    second = live_session(main, case.root, "Apple_Terminal")
    assert "진님 답할 질문" in second
    assert FONT not in second, "the hint is shown once, not on every start"


@pytest.mark.parametrize("term_program, language", [
    ("iTerm.app", "ko"),
    (None, "ko"),
    ("Apple_Terminal", "en"),
])
def test_other_terminals_and_english_get_no_hint_and_keep_it_for_later(
        git_history, tmp_path, monkeypatch, term_program, language):
    case, main = korean_view(git_history, tmp_path, monkeypatch, language=language)
    for _ in range(2):
        assert FONT not in live_session(main, case.root, term_program), (
            "only Korean views in Terminal.app show the font hint")
    if language == "ko":
        # Runs elsewhere do not use up the hint: the first Terminal.app run still gets it.
        assert FONT in live_session(main, case.root, "Apple_Terminal")


def test_printed_frame_and_its_width_do_not_depend_on_the_terminal(git_history, tmp_path, monkeypatch, capsys):
    from test_view_done_state import cell_count

    case, main = korean_view(git_history, tmp_path, monkeypatch)
    frames = []
    for term_program in ("iTerm.app", "Apple_Terminal", "Apple_Terminal"):
        monkeypatch.setenv("TERM_PROGRAM", term_program)
        assert main(["gate", "watch", "--once", "--repo-root", str(case.root)]) == 0
        frames.append(capsys.readouterr().out)
    assert "진님 답할 질문" in frames[0]
    assert frames[1] == frames[0] == frames[2], (
        "Terminal.app gets the same printed frame: same cell widths, no extra lines")
    plain = screen_text(frames[1].encode())
    assert all(sum(cell_count(ch) for ch in line) <= COLUMNS for line in plain.splitlines()), (
        "Korean rows still fit the 120-cell window")
