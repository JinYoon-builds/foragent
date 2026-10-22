"""Acceptance test for lane-talk-view. Locked by Foragent; do not edit."""

from datetime import timedelta
import json

from test_claude_sessions import sessions_offline
from test_local_truth import record_gh
from test_orch_review_handoff import bind_cli, make_case, tick_context
from test_skills_autosync import isolated_home
from test_squirrel_running import plain_frame
from test_view_done_state import cell_count


def talk_view(git_history, tmp_path, monkeypatch, capsys, *, talking=False, finished=False):
    from foragent.scheduler import tick
    from foragent.watch import collect

    case = make_case(git_history, tmp_path, monkeypatch, language="ko", skill_libraries=[],
                     test_command="true", lanes={
                         "orch": {"paths": ["src/**"], "executor": "claude"},
                         "empty": {"paths": ["empty/**"], "executor": "codex"},
                     })
    case.tracker.add_labels(1, ["lane:orch"])
    case.store.update_run(1, lane="orch", phase="implementing")
    # A larger issue number is older: the last issue comes from run history,
    # rather than choosing the largest number or treating every idle lane as empty.
    case.clock[0] -= timedelta(minutes=10)
    case.tracker.add_issue(7, "Earlier task", labels=["lane:orch"], state="closed")
    case.store.start_run(7, branch="foragent/issue-7", worktree=case.store.worktree_path(7), lane="orch")
    case.store.finish_run(7, "merged")
    case.clock[0] += timedelta(minutes=10)
    if finished:
        case.store.finish_run(1, "merged")
        case.tracker.close_issue(1)
        case.tracker.remove_labels(1, ["running"])
    main = bind_cli(case, monkeypatch)
    gh = record_gh(monkeypatch)
    tick(tick_context(case, start_runs=False))
    assert main(["gate", "status", "--json", "--repo-root", str(case.root)]) == 0
    status = json.loads(capsys.readouterr().out)
    # Test the renderer's snapshot contract independently of session persistence.
    for run in status["active_runs"]:
        run.update(session_id="offline-view-session", session_executor="claude", talking=talking)
    snapshot = collect(status, case.tracker, case.store, case.config, case.clock[0])
    return case, main, snapshot, gh


def squirrel_cells(snapshot, case, width, lane="orch"):
    from foragent.watch import hit_test, render_frame

    frame = plain_frame(render_frame(snapshot, case.config, case.root, width=width))
    cells = [(x, y) for y, line in enumerate(frame.splitlines()) for x in range(width)
             if hit_test(snapshot, case.config, case.root, x=x, y=y, width=width) == ("squirrel", lane)]
    assert cells, f"The {lane} squirrel needs a public, clickable hit region"
    return frame, cells


def patch_talk(monkeypatch):
    from foragent import cli, lanes, watch

    calls = []

    def talk(*args, **kwargs):
        calls.append((args, kwargs))
        return 0

    original = lanes.talk
    # Cover both direct imports and module lookups at the same public operation.
    for module in (lanes, cli, watch):
        for name, value in tuple(vars(module).items()):
            if value is original:
                monkeypatch.setattr(module, name, talk)
    return calls


def test_lane_talk_view_current_squirrel_click_calls_the_same_talk_function_as_cli(
    git_history, tmp_path, monkeypatch, capsys,
):
    from foragent.watch import handle_mouse

    case, main, snapshot, gh = talk_view(git_history, tmp_path, monkeypatch, capsys)
    calls = patch_talk(monkeypatch)
    assert main(["lanes", "talk", "1", "--repo-root", str(case.root)]) == 0
    assert len(calls) == 1
    cli_call = calls.pop()
    _, cells = squirrel_cells(snapshot, case, 56)
    x, y = cells[0]
    handle_mouse(snapshot, case.config, case.root, x=x, y=y, width=56)
    assert calls == [cli_call] and gh == []


def test_lane_talk_view_finished_lane_squirrel_talks_to_its_last_issue(
    git_history, tmp_path, monkeypatch, capsys,
):
    from foragent.watch import handle_mouse

    case, main, snapshot, gh = talk_view(git_history, tmp_path, monkeypatch, capsys, finished=True)
    assert snapshot.status["lanes"]["orch"]["issue"] is None
    calls = patch_talk(monkeypatch)
    assert main(["lanes", "talk", "1", "--repo-root", str(case.root)]) == 0
    assert len(calls) == 1
    cli_call = calls.pop()
    _, cells = squirrel_cells(snapshot, case, 56)
    x, y = cells[0]
    handle_mouse(snapshot, case.config, case.root, x=x, y=y, width=56)
    assert calls == [cli_call] and gh == []


def test_lane_talk_view_talking_badge_is_above_squirrel_and_text_mode_names_the_state(
    git_history, tmp_path, monkeypatch, capsys,
):
    from foragent.watch import render_frame

    case, _, snapshot, gh = talk_view(git_history, tmp_path, monkeypatch, capsys, talking=True)
    for width in (56, 44):
        frame = plain_frame(render_frame(snapshot, case.config, case.root, width=width))
        assert "💬" in frame, "A talking run needs its speech badge"
        _, cells = squirrel_cells(snapshot, case, width)
        lines = frame.splitlines()
        top = min(y for x, y in cells)
        left, right = min(x for x, y in cells), max(x for x, y in cells)
        badges = [(cell_count(line[:index]), y)
                  for y, line in enumerate(lines[:top])
                  for index, char in enumerate(line) if char == "💬"]
        assert any(left <= x <= right and top - 2 <= y < top for x, y in badges), frame
    plain = plain_frame(render_frame(snapshot, case.config, case.root, width=40))
    assert "대화 중 💬" in plain, plain
    for run in snapshot.status["active_runs"]:
        run["talking"] = False
    assert "💬" not in plain_frame(render_frame(snapshot, case.config, case.root, width=56))
    assert "대화 중 💬" not in plain_frame(render_frame(snapshot, case.config, case.root, width=40))
    assert gh == []


def test_lane_talk_view_lane_with_no_current_or_past_issue_does_nothing_on_click(
    git_history, tmp_path, monkeypatch, capsys,
):
    from foragent.watch import handle_mouse

    case, _, snapshot, gh = talk_view(git_history, tmp_path, monkeypatch, capsys)
    assert snapshot.status["lanes"]["empty"]["issue"] is None
    assert not snapshot.done.get("empty")
    calls = patch_talk(monkeypatch)
    _, cells = squirrel_cells(snapshot, case, 56, lane="empty")
    x, y = cells[0]
    handle_mouse(snapshot, case.config, case.root, x=x, y=y, width=56)
    assert calls == [] and gh == []
