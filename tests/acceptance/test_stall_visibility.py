"""Acceptance test for stall-visibility (D54). Locked by Foragent; do not edit.

r53/D112: the dashboard's main screen shows the acorn only; a stalled or dead worker is
explained in the big squirrel's bubble after a click on that acorn.
"""

import os
import subprocess
from dataclasses import replace
from datetime import timedelta


def _lane_rows(output):
    from test_squirrel_running import plain_frame
    return [line for line in plain_frame(output).splitlines() if '▌' in line or '[' in line and '#' in line]


def _api_row(output):
    from test_lane_m2_contract import acorn_row,bubble
    return acorn_row(output,1)+'\n'+bubble(output)


def _dashboard(main, case, capsys, *, number=1, width=160):
    """`fa gate watch --once` as printed, and that same dashboard after a click on acorn #number.

    The click goes through the public render_frame/handle_mouse pair on the snapshot the
    dashboard itself rendered (captured at the render_frame patch point)."""
    import pytest
    from foragent import watch
    from test_acorn_interact import hit_positions

    seen, real = {}, watch.render_frame

    def capture(snapshot, config, root, **kwargs):
        seen.update(snapshot=snapshot, config=config, root=root)
        return real(snapshot, config, root, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(watch, "render_frame", capture)
        assert main(["gate", "watch", "--once", "--repo-root", str(case.root)]) == 0
    printed = capsys.readouterr().out
    state, layout = {"store": case.store}, {}
    draw = lambda: real(seen["snapshot"], seen["config"], seen["root"], width=width, state=state,
                        layout=layout, now=case.clock[0])
    hits = hit_positions(layout, draw(), width)
    assert ("acorn", number) in hits, "the dashboard keeps a clickable acorn for the issue"
    watch.handle_mouse("left", *hits[("acorn", number)], layout, case.store, seen["config"], state=state)
    return printed, draw()


def _main_row(printed, number=1):
    from test_lane_m2_contract import acorn_row
    return acorn_row(printed, number)


def test_dead_runner_shows_as_stalled_at_once(git_history, tmp_path, monkeypatch, capsys):
    from foragent.scheduler import tick
    from test_orch_review_handoff import tick_context
    from test_view_status import status_case

    case, main = status_case(git_history, tmp_path, monkeypatch)
    tick(tick_context(case, start_runs=False))
    process = subprocess.Popen(["true"])
    process.wait()
    now = case.clock[0].isoformat()
    case.store.update_run(1, pid=process.pid, last_activity_at=now, last_progress_at=now)
    printed, opened = _dashboard(main, case, capsys)
    assert "no worker" not in _main_row(printed), "D112: the warning is not on the main screen"
    assert "#1" in _api_row(opened) and "no worker" in _api_row(opened)


def test_stalled_mark_follows_stall_minutes(git_history, tmp_path, monkeypatch, capsys):
    from foragent import cli
    from foragent.scheduler import tick
    from test_orch_review_handoff import tick_context
    from test_view_status import status_case

    case, main = status_case(git_history, tmp_path, monkeypatch)
    tick(tick_context(case, start_runs=False))
    config = replace(case.ctx.config, stall_minutes=3)
    monkeypatch.setattr(cli, "load_config", lambda root: config)
    recent = (case.clock[0] - timedelta(minutes=4)).isoformat()
    case.store.update_run(1, pid=os.getpid(), last_activity_at=recent, last_progress_at=recent)
    printed, opened = _dashboard(main, case, capsys)
    assert "looks stalled" not in _main_row(printed), "D112: the warning is not on the main screen"
    assert "#1" in _api_row(opened) and "looks stalled 4 min" in _api_row(opened)

    fresh = (case.clock[0] - timedelta(minutes=1)).isoformat()
    case.store.update_run(1, last_activity_at=fresh, last_progress_at=fresh)
    printed, opened = _dashboard(main, case, capsys)
    assert "looks stalled" not in _api_row(opened) and "looks stalled" not in printed


def test_notification_line_names_the_issue_and_the_reason(git_history, tmp_path, monkeypatch):
    from foragent.actions import flag
    from foragent.config import Config
    from foragent.inspect_actions import notify
    from foragent.inspect_data import InspectContext
    from foragent.reasons import Reason
    from foragent.scheduler import tick
    from test_orch_review_handoff import make_case, tick_context

    case = make_case(git_history, tmp_path, monkeypatch)
    flag(case.ctx, 1, Reason.STALLED, "Runner stalled again after one restart.")
    case.store.finish_run(1, outcome="stalled")
    case.clock[0] += timedelta(minutes=case.config.orchestrator_timeout_minutes + 1)
    tick(tick_context(case, start_runs=False))
    assert "needs-human" in case.tracker.get_issue(1).labels
    sent = []
    ctx = InspectContext(case.tracker, case.store, "owner/repo", Config(repo="owner/repo"), lambda: case.clock[0])
    line = notify(ctx, notifier=lambda line, title: sent.append(line))
    assert sent and line == sent[0]
    assert "#1" in line and "Implement feature" in line, line

