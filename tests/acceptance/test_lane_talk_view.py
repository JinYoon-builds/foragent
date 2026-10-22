"""Acceptance test for lane-talk-view. Locked by Foragent; do not edit."""

import json
import unicodedata

from test_local_truth import record_gh
from test_orch_review_handoff import bind_cli, make_case
from test_skills_autosync import isolated_home


def talk_view(git_history, tmp_path, monkeypatch, capsys, *, talking=False, idle=False):
    from foragent.watch import collect

    case = make_case(git_history, tmp_path, monkeypatch, language="ko", skill_libraries=[],
                     test_command="true", lanes={"orch": {"paths": ["src/**"], "executor": "claude"}})
    case.tracker.add_labels(1, ["lane:orch"])
    case.store.update_run(1, lane="orch", phase="implementing")
    if idle:
        case.store.finish_run(1, "merged")
        case.tracker.close_issue(1)
    main = bind_cli(case, monkeypatch)
    gh = record_gh(monkeypatch)
    assert main(["gate", "status", "--json", "--repo-root", str(case.root)]) == 0
    status = json.loads(capsys.readouterr().out)
    # Feed the renderer its public snapshot contract without depending on D55's
    # not-yet-added Run fields or opening a terminal to arrange display state.
    for run in status["active_runs"]:
        run.update(session_id="offline-view-session", talking=talking)
    snapshot = collect(status, case.tracker, case.store, case.config, case.clock[0])
    return case, main, snapshot, gh


def squirrel_cells(snapshot, case, width):
    from foragent.watch import hit_test, render_frame

    frame = render_frame(snapshot, case.config, case.root, width=width)
    cells = [(x, y) for y, line in enumerate(frame.splitlines()) for x in range(width)
             if hit_test(snapshot, case.config, case.root, x=x, y=y, width=width) == ("squirrel", "orch")]
    assert cells, "The lane squirrel needs a public, clickable hit region"
    return frame, cells


def test_lane_talk_view_squirrel_click_dispatches_to_the_same_talk_function_as_cli(
    git_history, tmp_path, monkeypatch, capsys,
):
    from foragent import cli, lanes, watch
    from foragent.watch import handle_mouse

    case, main, snapshot, gh = talk_view(git_history, tmp_path, monkeypatch, capsys)
    calls = []

    def talk(*args, **kwargs):
        calls.append((args, kwargs))
        return 0

    original = lanes.talk
    # Cover direct imports as well as module lookups at the public boundary.
    for module in (lanes, cli, watch):
        for name, value in tuple(vars(module).items()):
            if value is original:
                monkeypatch.setattr(module, name, talk)
    # The command and click must share this public operation, not implement two
    # subtly different attach/state paths. Compare the complete call arguments.
    assert main(["lanes", "talk", "1", "--repo-root", str(case.root)]) == 0
    assert len(calls) == 1
    cli_call = calls.pop()
    _, cells = squirrel_cells(snapshot, case, 56)
    x, y = cells[0]
    handle_mouse(snapshot, case.config, case.root, x=x, y=y, width=56)
    assert calls == [cli_call] and gh == []


def test_lane_talk_view_talking_badge_is_above_its_squirrel_and_text_mode_names_the_state(
    git_history, tmp_path, monkeypatch, capsys,
):
    from foragent.watch import render_frame

    case, _, snapshot, gh = talk_view(git_history, tmp_path, monkeypatch, capsys, talking=True)
    frame, cells = squirrel_cells(snapshot, case, 56)
    lines = frame.splitlines()
    top = min(y for x, y in cells)
    left, right = min(x for x, y in cells), max(x for x, y in cells)
    badges = []
    for y, line in enumerate(lines[:top]):
        x = 0
        for char in line:
            if char == "💬":
                badges.append((x, y))
            x += 0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in "WF" else 1
    assert any(left <= x <= right for x, y in badges), frame
    plain = render_frame(snapshot, case.config, case.root, width=40)
    assert "대화 중 💬" in plain, plain
    assert gh == []


def test_lane_talk_view_idle_squirrel_click_does_not_talk(
    git_history, tmp_path, monkeypatch, capsys,
):
    from foragent import lanes
    from foragent.watch import handle_mouse

    case, _, snapshot, gh = talk_view(git_history, tmp_path, monkeypatch, capsys, idle=True)
    calls = []
    monkeypatch.setattr(lanes, "talk", lambda *args, **kwargs: calls.append((args, kwargs)))
    _, cells = squirrel_cells(snapshot, case, 56)
    x, y = cells[0]
    handle_mouse(snapshot, case.config, case.root, x=x, y=y, width=56)
    assert calls == [] and gh == []
