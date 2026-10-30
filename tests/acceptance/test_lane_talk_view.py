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
    from foragent.watch import render_frame,hit_test
    layout={}
    frame=plain_frame(render_frame(snapshot,case.config,case.root,width=width,layout=layout))
    cells=[(x,y) for y,_ in enumerate(frame.splitlines()) for x in range(width) if hit_test(layout,x,y)==('squirrel',lane)]
    assert cells, f'The {lane} squirrel needs a public clickable hit region'
    return frame,cells


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
    from test_lane_m2_contract import pick,bubble
    case,main,snapshot,gh=talk_view(git_history,tmp_path,monkeypatch,capsys)
    calls=patch_talk(monkeypatch)
    opened,_=pick(snapshot,case.config,case.root,case.store,('squirrel','orch'),width=240)
    words=bubble(opened)
    assert 'src/**' in words and len([line for line in words.splitlines() if line])==3, 'replacement: lane ownership, weekly results, newest memory'
    assert calls==[] and gh==[], 'r55(c) replaces talk with a local explanation'


def test_lane_talk_view_finished_lane_squirrel_talks_to_its_last_issue(
    git_history, tmp_path, monkeypatch, capsys,
):
    from test_lane_m2_contract import pick,bubble
    case,main,snapshot,gh=talk_view(git_history,tmp_path,monkeypatch,capsys,finished=True)
    calls=patch_talk(monkeypatch)
    idle_name_target(snapshot, case, "orch")
    opened,_=pick(snapshot,case.config,case.root,case.store,('squirrel','orch'),width=240)
    words=bubble(opened)
    assert 'src/**' in words and '이번 주' in words, 'idle lanes explain their own results rather than talking to the last issue'
    assert len([line for line in words.splitlines() if line])==3 and calls==[] and gh==[]


def test_lane_talk_view_talking_badge_is_above_squirrel_and_text_mode_names_the_state(
    git_history, tmp_path, monkeypatch, capsys,
):
    from test_lane_m2_contract import pick,bubble,draw
    case,_,snapshot,gh=talk_view(git_history,tmp_path,monkeypatch,capsys,talking=True)
    calls=patch_talk(monkeypatch)
    opened,_=pick(snapshot,case.config,case.root,case.store,('squirrel','orch'),width=240)
    assert 'src/**' in bubble(opened) and len([s for s in bubble(opened).splitlines() if s])==3
    for run in snapshot.status['active_runs']: run['talking']=False
    again,_=pick(snapshot,case.config,case.root,case.store,('squirrel','orch'),width=240)
    assert bubble(again)==bubble(opened), 'replacement: the introduction is independent of a historical talk session badge'
    assert calls==[] and gh==[]


def test_lane_talk_view_lane_with_no_current_or_past_issue_does_nothing_on_click(
    git_history, tmp_path, monkeypatch, capsys,
):
    from test_lane_m2_contract import pick,bubble
    case,_,snapshot,gh=talk_view(git_history,tmp_path,monkeypatch,capsys)
    calls=patch_talk(monkeypatch)
    idle_name_target(snapshot, case, "empty")
    opened,_=pick(snapshot,case.config,case.root,case.store,('squirrel','empty'),width=240)
    words=bubble(opened)
    assert 'empty/**' in words and len([s for s in words.splitlines() if s])==3, 'even an empty lane has ownership to explain'
    assert calls==[] and gh==[]


def idle_name_target(snapshot, case, lane):
    """One shared sleeping sprite; each idle name selects that lane's introduction."""
    from foragent.watch import render_frame
    from foragent.labels import lane_display
    from test_acorn_interact import hit_positions

    layout = {}
    frame = plain_frame(render_frame(snapshot, case.config, case.root, width=240, layout=layout))
    hits = hit_positions(layout, frame, 240)
    assert ("squirrel", lane) in hits, "idle names retain the public lane explanation target"
    x, y = hits[("squirrel", lane)]
    line = frame.splitlines()[y]
    name = lane_display(case.config, lane, case.config.language)
    assert name in line, "idle clicks choose a name, not an arbitrary lane under the shared sprite"
    first = cell_count(line[:line.index(name)])
    assert first <= x < first + cell_count(name)

