"""The one-time Terminal.app font hint (#1793): detection, the record and the live alert row."""

from dataclasses import replace
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "acceptance"))

from foragent import capacity, watch, watch_hint  # noqa: E402
# The locked view test's offline terminal (no network, no executors, a fixed 120x30
# environment) for the real pseudo-terminal case; it isolates HOME for every test here.
from test_terminal_app_hangul_hint import offline_terminal  # noqa: E402,F401
from test_skills_autosync import isolated_home  # noqa: E402,F401

TERMINAL = {"TERM_PROGRAM": "Apple_Terminal"}


@pytest.fixture(autouse=True)
def machine(tmp_path, monkeypatch):
    root = tmp_path / "machine"
    monkeypatch.setattr(capacity, "machine_root", lambda: root)
    return root


@pytest.mark.parametrize("environ, language, shown", [
    (TERMINAL, "ko", True),
    (TERMINAL, "ko-KR", True),
    (TERMINAL, "en", False),
    ({"TERM_PROGRAM": "iTerm.app"}, "ko", False),
    ({"TERM_PROGRAM": "tmux"}, "ko", False),
    ({}, "ko", False),
])
def test_only_korean_in_terminal_app_is_due(environ, language, shown):
    assert (watch_hint.due(language, environ) is not None) is shown


def test_only_a_confirmed_hold_ends_the_hint_and_a_bad_record_is_taken_over(machine):
    forms = watch_hint.due("ko", TERMINAL)
    assert all("D2Coding" in form and "github.com/naver/d2codingfont" in form for form in forms)
    assert watch_hint.due("ko", TERMINAL) == forms, "reading alone never uses up the hint"
    held = watch_hint.hold()
    assert isinstance(held, watch_hint.Hold)
    held.release()
    assert watch_hint.due("ko", TERMINAL) == forms, "holding without confirming uses up nothing"
    held = watch_hint.hold()
    assert held.confirm() is True
    assert json.loads((machine / watch_hint.RECORD).read_text())["shown_at"]
    held.release()
    assert watch_hint.due("ko", TERMINAL) is None
    assert watch_hint.hold() == watch_hint.SHOWN, "once confirmed, no view shows it again"
    (machine / watch_hint.RECORD).write_text("not json")
    assert watch_hint.due("ko", TERMINAL) == forms
    assert isinstance(watch_hint.hold(), watch_hint.Hold)


def test_simultaneous_holds_give_the_hint_to_one_view(machine):
    """Class: check-then-act. Only the lock holder may show it; the others wait their turn."""
    import threading

    results, start = [], threading.Barrier(6)

    def take():
        start.wait(timeout=2)
        results.append(watch_hint.hold())

    threads = [threading.Thread(target=take) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    holds = [result for result in results if isinstance(result, watch_hint.Hold)]
    assert len(holds) == 1 and results.count(watch_hint.BUSY) == 5
    holds[0].release()
    assert isinstance(watch_hint.hold(), watch_hint.Hold), "a view that let go leaves it for the next"


def test_a_failed_write_never_stops_anything(machine):
    machine.parent.mkdir(parents=True, exist_ok=True)
    machine.write_text("a file where the folder should be")
    held = watch_hint.hold()
    assert isinstance(held, watch_hint.Hold), "this session still shows it"
    assert held.confirm() is False, "not recorded, so a later start shows it again"
    assert watch_hint.due("ko", TERMINAL) is not None


@pytest.mark.parametrize("width, form", [(120, 0), (100, 1), (80, 1), (60, None)])
def test_only_a_form_that_fits_whole_is_chosen(width, form):
    forms = watch_hint.due("ko", TERMINAL)
    assert watch_hint.fitting(forms, width) == (None if form is None else forms[form])


def live(tmp_path, monkeypatch, keys, language="ko", term="Apple_Terminal", width=120, height=30,
         resized=lambda height, width: False, addstr=None, real=None):
    import curses
    from types import SimpleNamespace
    from unittest.mock import Mock

    from test_squirrel_running import running_snapshot
    from foragent import watch_terminal
    from foragent.state import StateStore

    snapshot, config = running_snapshot()
    config = replace(config, language=language)
    monkeypatch.setenv("TERM_PROGRAM", term)
    screen = Mock()
    screen.getmaxyx.return_value = (height, width) if isinstance(width, int) else None
    if not isinstance(width, int):
        screen.getmaxyx.side_effect = [(height, value) for value in width] + [(height, width[-1])] * 50
    screen.getch.side_effect = keys if not callable(keys) else (lambda: keys())
    screen.addstr.side_effect = addstr
    monkeypatch.setattr(watch, "time", SimpleNamespace(monotonic=lambda: 0))
    monkeypatch.setattr(watch, "_installed_build", lambda: "unknown")
    monkeypatch.setattr(curses, "has_colors", lambda: False)
    monkeypatch.setattr(curses, "curs_set", lambda *args: None)
    monkeypatch.setattr(curses, "mousemask", lambda *args: None, raising=False)
    monkeypatch.setattr(curses, "mouseinterval", lambda *args: None, raising=False)
    monkeypatch.setattr(curses, "is_term_resized", resized, raising=False)
    monkeypatch.setattr(watch_terminal, "_real_size", lambda: real)
    monkeypatch.setattr(watch.sys, "stdout", __import__("io").StringIO())
    watch_terminal._terminal(screen, lambda: snapshot, snapshot, config, tmp_path,
                             lambda: snapshot.captured_at, StateStore(tmp_path))
    return [call.args for call in screen.addstr.call_args_list if "D2Coding" in call.args[2]]


def test_a_window_too_narrow_shows_nothing_and_keeps_the_hint(tmp_path, monkeypatch, machine):
    """Class: premature consumption. A cut address must not count as shown."""
    assert live(tmp_path, monkeypatch, [-1, ord("q")], width=60) == []
    assert not (machine / watch_hint.RECORD).exists()
    short = watch_hint.due("ko", TERMINAL)[1]
    painted = live(tmp_path, monkeypatch, [-1, ord("q")], width=[60, 80])
    assert [args[2] for args in painted] == [short], (
        "widened to 80 columns: the short form is painted whole, then recorded")
    assert (machine / watch_hint.RECORD).exists()


def test_a_view_that_loses_the_claim_paints_no_hint(tmp_path, monkeypatch, machine):
    """Class: check-then-act. Another view shows it now, or showed it after this one's first look."""
    forms = watch_hint.due("ko", TERMINAL)
    monkeypatch.setattr(watch_hint, "due", lambda language: forms)
    other = watch_hint.hold()
    assert live(tmp_path, monkeypatch, [-1, ord("q")]) == [], "busy: another view shows it now"
    assert not (machine / watch_hint.RECORD).exists()
    other.confirm()
    other.release()
    assert live(tmp_path, monkeypatch, [-1, ord("q")]) == [], "shown: another view saw it whole"


@pytest.mark.parametrize("key, resized", [
    ("resize", lambda height, width: False),
    (-1, lambda height, width: width != 60),
    (-1, None),
])
def test_a_resize_before_the_hint_was_seen_records_nothing(tmp_path, monkeypatch, machine, key, resized):
    """Class: premature consumption. The frame was painted at 120 columns, then the window
    became 60 before the person could read it: nothing is recorded and a later start shows it."""
    import curses

    key = curses.KEY_RESIZE if key == "resize" else key
    if resized is None:  # curses cannot tell: counted as resized
        def resized(height, width):
            raise curses.error("unknown")
    painted = live(tmp_path, monkeypatch, [key, -1, ord("q")], width=[120, 60], resized=resized)
    assert len(painted) == 1 and painted[0][2] == watch_hint.due("ko", TERMINAL)[0]
    assert not (machine / watch_hint.RECORD).exists()
    assert live(tmp_path, monkeypatch, [-1, ord("q")])
    assert (machine / watch_hint.RECORD).exists(), "seen whole at 120 columns: recorded"


def test_a_hint_painted_with_an_error_is_not_seen(tmp_path, monkeypatch, machine):
    """Class: premature consumption. curses could not draw the hint row."""
    import curses

    def addstr(y, x, value, attribute):
        if "D2Coding" in value:
            raise curses.error("cut")

    assert live(tmp_path, monkeypatch, [-1, ord("q")], addstr=addstr)
    assert not (machine / watch_hint.RECORD).exists()


def test_live_view_shows_the_hint_on_the_bottom_row_until_the_first_key(tmp_path, monkeypatch, machine):
    painted = live(tmp_path, monkeypatch, [-1, ord("j"), ord("q")])
    assert len(painted) == 2 and len({args[0] for args in painted}) == 1, (
        "the alert row shows it for the two frames before the key, none after it")
    assert (machine / watch_hint.RECORD).exists(), "painting the hint records it"
    assert live(tmp_path, monkeypatch, [ord("q")]) == [], "a later start does not repeat it"


@pytest.mark.parametrize("language, term", [("en", "Apple_Terminal"), ("ko", "iTerm.app")])
def test_live_view_elsewhere_neither_shows_nor_uses_up_the_hint(tmp_path, monkeypatch, machine, language, term):
    assert live(tmp_path, monkeypatch, [ord("q")], language=language, term=term) == []
    assert not (machine / watch_hint.RECORD).exists()


@pytest.mark.parametrize("height", [1, 2, 3])
def test_a_screen_too_low_for_the_alert_row_keeps_the_hint(tmp_path, monkeypatch, machine, height):
    """Class: premature consumption. A hint the placement left off screen is not shown."""
    painted = live(tmp_path, monkeypatch, [-1, ord("q")], height=height)
    assert (machine / watch_hint.RECORD).exists() == bool(painted)


def test_a_hint_the_placement_drops_is_not_claimed(tmp_path, monkeypatch, machine):
    """Class: premature consumption. `_place` returns rows without the alert when no
    footer is pinned; the record follows what reaches the screen, not what was built."""
    from foragent import watch_terminal

    real = watch_terminal._place
    monkeypatch.setattr(watch_terminal, "_place",
                        lambda visible, row_map, footer, height, alert, layout:
                        real(visible, row_map, 0, height, alert, layout))
    assert live(tmp_path, monkeypatch, [-1, ord("q")]) == []
    assert not (machine / watch_hint.RECORD).exists()


def test_confirming_again_keeps_the_first_record(machine):
    from datetime import datetime, timezone

    held = watch_hint.hold()
    first, later = (datetime(2026, 11, 4, hour, tzinfo=timezone.utc) for hour in (9, 10))
    assert held.confirm(first) and held.confirm(later)
    assert json.loads((machine / watch_hint.RECORD).read_text())["shown_at"] == first.isoformat()


def test_the_first_key_lets_other_views_take_their_turn(tmp_path, monkeypatch, machine):
    """The view that dismissed an unseen hint must not keep other views waiting."""
    seen = []
    keys = iter([ord("j"), None, ord("q")])

    def getch_then_look():
        key = next(keys)
        if key is None:  # after the key press, while this view still runs
            other = watch_hint.hold()
            seen.append(other)
            if isinstance(other, watch_hint.Hold):
                other.release()
            return -1
        return key

    assert live(tmp_path, monkeypatch, getch_then_look, resized=lambda height, width: True)
    assert len(seen) == 1 and isinstance(seen[0], watch_hint.Hold)


def test_a_real_window_that_differs_from_what_curses_believes_records_nothing(tmp_path, monkeypatch, machine):
    """curses trusts LINES/COLUMNS over the terminal: the real size decides whether it was seen."""
    assert live(tmp_path, monkeypatch, [-1, ord("q")], real=(30, 60))
    assert not (machine / watch_hint.RECORD).exists()
    assert live(tmp_path, monkeypatch, [-1, ord("q")], real=(30, 120))
    assert (machine / watch_hint.RECORD).exists()


def test_a_real_terminal_shrunk_right_after_the_hint_records_nothing(git_history, tmp_path, monkeypatch):
    """The review's case in a real pseudo-terminal: drawn at 120 columns, the window becomes
    60 within the first key wait. Nothing is recorded; the next whole showing is."""
    import fcntl
    import os
    import pty
    import select
    import struct
    import termios
    import time

    from test_terminal_app_hangul_hint import FONT, korean_view, live_session

    case, main = korean_view(git_history, tmp_path, monkeypatch)
    pid, fd = pty.fork()
    if pid == 0:
        code = 1
        try:
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 120, 0, 0))
            sys.stdin = open(0, "r", encoding="utf-8", closefd=False)
            sys.stdout = open(1, "w", encoding="utf-8", closefd=False, buffering=1)
            sys.stderr = open(2, "w", encoding="utf-8", closefd=False, buffering=1)
            os.environ["TERM_PROGRAM"] = "Apple_Terminal"
            code = main(["gate", "watch", "--repo-root", str(case.root)]) or 0
        finally:
            os._exit(code)
    raw, shrunk, quit_at, deadline = b"", False, None, time.monotonic() + 30
    try:
        while time.monotonic() < deadline:
            if select.select([fd], [], [], 0.05)[0]:
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    break
                if not data:
                    break
                raw += data
            if not shrunk and FONT.encode() in raw:
                fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 60, 0, 0))
                shrunk, quit_at = True, time.monotonic() + 2
            if quit_at is not None and time.monotonic() >= quit_at:
                os.write(fd, b"q")
                quit_at = float("inf")
    finally:
        _, status = os.waitpid(pid, 0)
        os.close(fd)
    assert shrunk and os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
    assert not (capacity.machine_root() / watch_hint.RECORD).exists()
    assert FONT in live_session(main, case.root, "Apple_Terminal"), "a later 120-column start shows it"
    assert FONT not in live_session(main, case.root, "Apple_Terminal")
