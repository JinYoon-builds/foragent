"""Acceptance test for acorn-interact. Locked by Foragent; do not edit."""

from dataclasses import replace
from io import StringIO

from test_local_truth import record_gh, snapshot_case
from test_skills_autosync import isolated_home
from test_squirrel_running import plain_frame, running_snapshot, view_offline


def interaction_snapshot(count=7):
    """Contract: render_frame fills caller-owned layout; handlers mutate state.

    State's issue is the open box number (or None), and scroll is a zero-based
    content offset. Mouse coordinates and hit_test are zero-based screen cells.
    """
    snapshot, config = running_snapshot()
    row = snapshot.status["lanes"]["work"]
    row.update(issue=3, title="Current task", running=[{"number": 3, "title": "Current task"}],
               queued=[{"number": n, "title": f"Task {n}"} for n in range(4, count + 1)])
    status = {**snapshot.status, "lanes": {"work": row},
              "summary": {"working": 1, "blocked": 0, "waiting_human": 0, "queued": count - 3},
              "active_runs": [{"number": 3, "lane": "work", "attempts": 1}]}
    return (replace(snapshot, status=status, done={"work": snapshot.done["work"]}, completed=2, total=count),
            replace(config, lanes={"work": config.lanes["work"]}))


def hit_positions(layout, frame, width):
    """Discover hit boxes from the actual public renderer, not a made-up layout."""
    from foragent.watch import hit_test

    hits = {}
    for y, _ in enumerate(plain_frame(frame).splitlines()):
        for x in range(width):
            target = hit_test(layout, x, y)
            if target is not None:
                hits.setdefault(target, (x, y))
    return hits


def test_acorn_interact_issue_box_reads_all_details_from_local_records_without_gh(
    git_history, tmp_path, monkeypatch,
):
    from foragent.watch import issue_box

    case = snapshot_case(git_history, tmp_path, monkeypatch)
    # The local body is the approved fixture body. A remote edit must stay invisible.
    case.tracker.update_issue(1, title="Remote title", body="Remote goal")
    calls = record_gh(monkeypatch)
    lines = issue_box(1, case.store, case.config)
    assert isinstance(lines, list) and all(isinstance(line, str) for line in lines)
    box = plain_frame("\n".join(lines))
    for value in ("#1", "Implement feature", "Api", "Implement the feature.",
                  "test -f feature.txt", "https://github.com/owner/repo/issues/1"):
        assert value in box, (value, box)
    assert "implementing" in box.lower() or "구현" in box
    assert "Remote title" not in box and "Remote goal" not in box
    assert calls == []


def test_acorn_interact_collapsed_neighbours_expand_all_and_more_hits_are_real(tmp_path):
    from foragent.state import StateStore
    from foragent.watch import handle_key, handle_mouse, hit_test, render_frame

    snapshot, config = interaction_snapshot()
    store, state, layout = StateStore(tmp_path / "state"), {}, {}
    collapsed = render_frame(snapshot, config, tmp_path, width=80, state=state, layout=layout)
    hits = hit_positions(layout, collapsed, 80)
    assert {target[1] for target in hits if target[0] == "acorn"} == {2, 3, 4}
    assert "▸ 4개 더" in plain_frame(collapsed)
    assert hit_test(layout, -1, -1) is None and hit_test(layout, 80, 0) is None
    x, y = hits[("more", "work")]
    handle_mouse("left", x, y, layout, store, config, state=state)
    expanded = render_frame(snapshot, config, tmp_path, width=80, state=state, layout=layout)
    assert "▾ 접기" in plain_frame(expanded)
    assert {target[1] for target in hit_positions(layout, expanded, 80) if target[0] == "acorn"} == set(range(1, 8))
    # Return to collapsed before exercising the independent expand-all shortcut.
    x, y = hit_positions(layout, expanded, 80)[("more", "work")]
    handle_mouse("left", x, y, layout, store, config, state=state)
    for expected in ("▾ 접기", "▸ 4개 더"):
        handle_key("a", store, config, state=state)
        assert expected in plain_frame(render_frame(snapshot, config, tmp_path, width=80, state=state))


def test_acorn_interact_wheel_and_j_k_scroll_without_clicks_and_show_position(tmp_path):
    from foragent.state import StateStore
    from foragent.watch import handle_key, handle_mouse, render_frame

    snapshot, config = interaction_snapshot(count=52)
    store, state, layout = StateStore(tmp_path / "state"), {}, {}
    handle_key("a", store, config, state=state)
    full = render_frame(snapshot, config, tmp_path, width=56, state=state, layout=layout)
    x, y = hit_positions(layout, full, 56)[("acorn", 3)]
    for key in ("j", "k"):
        before = state.get("scroll", 0)
        handle_key(key, store, config, state=state)
        assert state["scroll"] > before if key == "j" else state["scroll"] < before
    for event in ("wheel-down", "wheel-up"):
        state["scroll"] = 10
        handle_mouse(event, x, y, layout, store, config, state=state)
        assert state["scroll"] > 10 if event == "wheel-down" else state["scroll"] < 10
        assert state.get("issue") is None, "wheel events over an acorn must never open a box"
    state["scroll"] = 11
    visible = plain_frame(render_frame(snapshot, config, tmp_path, width=56, height=30,
                                        state=state, layout=layout))
    assert len(visible.splitlines()) == 30
    assert f"12–40/{len(plain_frame(full).splitlines())}줄" in visible
    assert "Option" in visible, "mouse mode must explain how to select text"
    state["scroll"] = 0
    handle_key("k", store, config, state=state)
    assert state["scroll"] == 0


def test_acorn_interact_left_click_toggles_box_and_x_closes_it(
    git_history, tmp_path, monkeypatch,
):
    from foragent.watch import handle_key, handle_mouse, render_frame

    case = snapshot_case(git_history, tmp_path, monkeypatch)
    snapshot, config = interaction_snapshot()
    # Use the existing local issue #1, visible after expanding the lane.
    state, layout = {}, {}
    handle_key("a", case.store, config, state=state)
    frame = render_frame(snapshot, config, case.root, width=80, state=state, layout=layout)
    x, y = hit_positions(layout, frame, 80)[("acorn", 1)]
    calls = record_gh(monkeypatch)
    handle_mouse("left", x, y, layout, case.store, config, state=state)
    assert state["issue"] == 1
    handle_mouse("left", x, y, layout, case.store, config, state=state)
    assert state.get("issue") is None
    handle_mouse("left", x, y, layout, case.store, config, state=state)
    handle_key("x", case.store, config, state=state)
    assert state.get("issue") is None and calls == []


def test_acorn_interact_live_numbers_use_osc8_and_live_setup_enters_alternate_screen(
    git_history, tmp_path, monkeypatch,
):
    import curses
    from foragent import watch
    from foragent.watch import osc8, terminal_setup_sequences

    url = "https://github.com/owner/repo/issues/1"
    assert osc8(url, "#1") == f"\x1b]8;;{url}\x1b\\#1\x1b]8;;\x1b\\"
    assert "\x1b[?1049h" in "".join(terminal_setup_sequences())
    case = snapshot_case(git_history, tmp_path, monkeypatch)
    from test_orch_review_handoff import bind_cli
    main = bind_cli(case, monkeypatch)
    # Capture status through the public CLI without a real tty.
    import json
    import contextlib
    status_output = StringIO()
    with contextlib.redirect_stdout(status_output):
        assert main(["gate", "status", "--json"]) == 0
    status = json.loads(status_output.getvalue())
    linked, setup_calls, painted = [], [], []
    original_setup = terminal_setup_sequences

    def link(url, text):
        linked.append((url, text))
        return osc8(url, text)

    def setup():
        setup_calls.append(True)
        return original_setup()

    class TTY(StringIO):
        def isatty(self):
            return True

    class Screen:
        def getmaxyx(self):
            return 100, 100

        def getch(self):
            return ord("q")

        def addstr(self, *args):
            painted.append(args)

        def keypad(self, value):
            pass

        def timeout(self, value):
            pass

        def erase(self):
            pass

        def refresh(self):
            pass

    output = TTY()
    monkeypatch.setattr(watch.sys, "stdin", TTY())
    monkeypatch.setattr(watch.sys, "stdout", output)
    monkeypatch.setattr(curses, "wrapper", lambda callback, *args: callback(Screen(), *args))
    monkeypatch.setattr(curses, "has_colors", lambda: False)
    monkeypatch.setattr(curses, "curs_set", lambda value: None)
    monkeypatch.setattr(curses, "mousemask", lambda value: (value, 0))
    monkeypatch.setattr(curses, "mouseinterval", lambda value: None)
    monkeypatch.setattr(watch, "osc8", link)
    monkeypatch.setattr(watch, "terminal_setup_sequences", setup)
    watch.run(lambda: status, case.tracker, case.store, case.config, case.root, now=lambda: case.clock[0])
    assert (url, "#1") in linked, "the live renderer must use the public OSC 8 builder"
    assert painted
    terminal_output = output.getvalue() + "".join(
        value for args in painted for value in args if isinstance(value, str))
    assert osc8(url, "#1") in terminal_output, "the linked number must actually reach the terminal"
    assert setup_calls and "\x1b[?1049h" in output.getvalue()
