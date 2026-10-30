"""Acceptance test for orch-panel. Locked by Foragent; do not edit."""

from dataclasses import replace
from datetime import timedelta
import json

from test_heartbeats import heartbeats_write
from test_local_truth import record_gh, snapshot_case
from test_orch_review_handoff import bind_cli
from test_skills_autosync import cli_exit, isolated_home
from test_squirrel_running import plain_frame, view_offline


def orchestrator_frame(case, main, capsys):
    from foragent.watch import collect, render_frame

    capsys.readouterr()
    assert main(["gate", "status", "--json", "--repo-root", str(case.root)]) == 0
    status = json.loads(capsys.readouterr().out)
    snapshot = collect(status, case.tracker, case.store, case.ctx.config, case.clock[0])
    return plain_frame(render_frame(snapshot, case.ctx.config, case.root, width=160, now=case.clock[0]))


def panel_case(git_history, tmp_path, monkeypatch):
    case = snapshot_case(git_history, tmp_path, monkeypatch)
    case.ctx.config = replace(case.config, language="ko")
    return case, bind_cli(case, monkeypatch)


def test_orch_panel_note_displays_now_why_next_inbox_and_recorded_orca_title(
    git_history, tmp_path, monkeypatch, capsys,
):
    from foragent import orch_inbox
    from test_lane_m2_contract import bubble
    case,main=panel_case(git_history,tmp_path,monkeypatch)
    values=('막힌 이슈를 보고 있어요','시험과 설명이 달라요','설명을 고쳐 다시 맡길게요')
    assert cli_exit(main,['orch','note','--now',values[0],'--why',values[1],'--next',values[2],'--orca-title','orch: foragent','--repo-root',str(case.root)])==0
    note=orch_inbox.read_note(case.store)
    assert (note['now'],note['why'],note['next'])==values and note['where']['title']=='orch: foragent'
    words=bubble(orchestrator_frame(case,main,capsys))
    assert '레인 1곳에서 이슈 1개를 작업하고 있어요.' in words and '기획 세션에서 물어볼게요.' in words
    assert values[1] not in words, 'r56 counts and planning-routed questions replace the technical note panel'


def test_orch_panel_note_displays_recorded_tmux_pane_without_contacting_tmux(
    git_history, tmp_path, monkeypatch, capsys,
):
    from foragent import orch_inbox
    from test_lane_m2_contract import bubble
    case,main=panel_case(git_history,tmp_path,monkeypatch)
    calls=record_gh(monkeypatch)
    assert cli_exit(main,['orch','note','--now','CI를 확인해요','--why','검사가 끝났어요','--next','머지할게요','--tmux-pane','%7','--repo-root',str(case.root)])==0
    note=orch_inbox.read_note(case.store)
    assert note['where']['kind']=='tmux' and '%7' in str(note['where'])
    assert '작업하고 있어요.' in bubble(orchestrator_frame(case,main,capsys))
    assert calls==[]


def test_orch_panel_without_note_has_evidence_line_and_no_empty_labels(
    git_history, tmp_path, monkeypatch, capsys,
):
    from test_lane_m2_contract import bubble
    case,main=panel_case(git_history,tmp_path,monkeypatch)
    calls=record_gh(monkeypatch)
    for age in (10,120):
        heartbeats_write(case.store,'orch',case.clock[0]-timedelta(seconds=age))
        frame=orchestrator_frame(case,main,capsys)
        assert '레인 1곳에서 이슈 1개를 작업하고 있어요.' in bubble(frame)
        assert not any(line.strip(' │').startswith(label+':') for line in frame.splitlines() for label in ('지금','이유','다음'))
    assert calls==[]

