"""Locked: waiting context stays visible in the lane view (r53/D112: in the acorn's bubble)."""
import json

from foragent import watch
from foragent.scheduler import tick
from test_local_truth import record_gh
from test_orch_review_handoff import bind_cli, make_case, tick_context
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401
from test_ssot_waiting_order import WAITING, REASON, CONDITION, ROLE


def test_lane_frame_shows_unapproved_wait_from_issue_snapshot(git_history, tmp_path, monkeypatch, capsys):
    """Reject showing only approved waits, or rendering a separate local reminder as truth."""
    case = make_case(git_history, tmp_path, monkeypatch, language='en', skill_libraries=[], test_command='true')
    case.store.finish_run(1, 'cancelled')
    case.tracker.remove_labels(1, ['running', 'approved'])
    case.tracker.update_issue(1, title=case.tracker.get_issue(1).title, body=case.tracker.get_issue(1).body + '\n' + WAITING)
    assert tick(tick_context(case, start_runs=False)).skipped is None
    main = bind_cli(case, monkeypatch)
    calls = record_gh(monkeypatch)
    capsys.readouterr()
    assert cli_exit(main, ['gate', 'status', '--json']) == 0
    status = json.loads(capsys.readouterr().out)
    snapshot = watch.collect(status, case.tracker, case.store, case.config, case.clock[0])
    frame = watch.render_frame(snapshot, case.config, case.root, width=240)
    for value in (REASON, CONDITION):  # The role name also appears in the roles line.
        assert value not in frame, f'D112: the main screen carries no per-issue wait text: {value}'
    # D112: a click on the acorn opens the bubble with the recorded wait.
    from test_lane_m2_contract import bubble, pick
    opened, _ = pick(snapshot, case.config, case.root, case.store, ('acorn', 1), width=240)
    words = bubble(opened)
    for value in (REASON, CONDITION, ROLE):
        assert value in words, f'Acorn bubble omitted the unapproved wait: {value}'
    assert calls == [], 'Rendering must reuse the gate snapshot'
