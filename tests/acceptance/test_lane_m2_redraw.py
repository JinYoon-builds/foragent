"""Acceptance test for lane-m2-redraw. Locked by Foragent; do not edit."""

from io import StringIO
from test_skills_autosync import isolated_home
from test_squirrel_running import single_worker, view_offline, utf8_terminal

def test_live_redraw_does_not_clear_and_keeps_half_second_ticks_and_five_second_data(tmp_path, monkeypatch):
    import curses
    from foragent import watch

    snapshot, config = single_worker()
    clock, refreshes, ticks, clears, timeouts = [0.0], [], [], [], []

    class Immediate:
        def result(self):
            return snapshot

        def done(self):
            return True

    class Worker:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def submit(self, refresh):
            refreshes.append(clock[0])
            refresh()
            return Immediate()

    class Screen:
        def getmaxyx(self):
            return 60, 160

        def timeout(self, value):
            timeouts.append(value)

        def getch(self):
            clock[0] += 0.5
            return ord("q") if clock[0] >= 10.5 else -1

        def erase(self):
            clears.append(clock[0])

        def clear(self):
            clears.append(clock[0])

        def clearok(self, value):
            if value:
                clears.append(clock[0])

        def __getattr__(self, name):
            return lambda *args, **kwargs: None

    original = watch._frame

    def frame(*args, **kwargs):
        ticks.append((clock[0], kwargs["tick"]))
        return original(*args, **kwargs)

    monkeypatch.setattr(curses, "has_colors", lambda: False)
    monkeypatch.setattr(curses, "curs_set", lambda value: None)
    monkeypatch.setattr(watch, "ThreadPoolExecutor", Worker)
    monkeypatch.setattr(watch.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(watch, "_frame", frame)
    monkeypatch.setattr(watch, "_installed_build", lambda: "offline")
    monkeypatch.setattr(watch.sys, "stdout", StringIO())
    watch._terminal(Screen(), lambda: snapshot, snapshot, config, tmp_path, lambda: snapshot.captured_at)
    assert len(clears) <= 1, "steady-state redraw must update in place instead of erasing each frame"
    assert timeouts and 400 <= timeouts[0] <= 600, "animate at about 2 fps"
    assert refreshes == [5.0, 10.0], "data refresh is independent of animation and due every five seconds"
    assert all(tick == int(at / 0.5) for at, tick in ticks), "animation ticks advance once per half second"
