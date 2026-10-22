"""Acceptance test for lane-view-d76. Locked by Foragent; do not edit."""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
import shlex
import shutil
import subprocess

from test_acorn_interact import hit_positions
from test_orch_inbox import add_request, inbox_case, inbox_command
from test_skills_autosync import isolated_home
from test_squirrel_running import plain_frame, view_offline
from test_view_done_state import done_snapshot


def lane_d76_case(tmp_path, monkeypatch):
    from foragent.tracker import FakeTracker

    case = inbox_case(tmp_path, monkeypatch)
    snapshot, config = done_snapshot(count=1)
    case.config = replace(config, repo="owner/repo")
    case.status = snapshot.status
    case.tracker = FakeTracker()
    return case


def lane_d76_frame(case, *, width=160, state=None, layout=None):
    from foragent.watch import collect, render_frame

    now = datetime.now(UTC)
    snapshot = collect(case.status, case.tracker, case.store, case.config, now)
    kwargs = {} if layout is None else {"state": state, "layout": layout}
    return render_frame(snapshot, case.config, case.root, width=width, now=now, **kwargs)


def lane_d76_opener(monkeypatch):
    """Record the terminal opener at its process boundary; never run Orca."""
    from foragent import watch, watch_open

    calls = []
    original = subprocess.run
    monkeypatch.setattr(shutil, "which", lambda name: "/offline/bin/orca" if name == "orca" else None)

    def record(argv, *args, **kwargs):
        argv = shlex.split(argv) if isinstance(argv, str) else [str(part) for part in argv]
        assert Path(argv[0]).name == "orca", f"unexpected external command: {argv}"
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="{}\n", stderr="")

    for module in (subprocess, watch, watch_open):
        for name, value in tuple(vars(module).items()):
            if value is original:
                monkeypatch.setattr(module, name, record)
    return calls


def test_lane_view_d76_big_squirrel_switches_to_the_orchestrator_terminal_recorded_in_its_note(
    tmp_path, monkeypatch,
):
    from foragent.watch import handle_mouse, hit_test

    case = lane_d76_case(tmp_path, monkeypatch)
    monkeypatch.setenv("ORCA_TERMINAL_HANDLE", "term_orch")
    assert inbox_command(case, "note", "--now", "요청을 확인해요", "--why", "결정이 도착했어요",
                         "--next", "주문을 만들게요", "--where-title", "orch: project") == 0
    # The current pane is the planning session, not the stored destination.
    monkeypatch.setenv("ORCA_TERMINAL_HANDLE", "term_planning")
    calls = lane_d76_opener(monkeypatch)
    for width in (160, 56, 44):
        state, layout = {}, {}
        frame = lane_d76_frame(case, width=width, state=state, layout=layout)
        hits = hit_positions(layout, frame, width)
        assert ("orchestrator", None) in hits, "the large squirrel needs its own hit region"
        x, y = hits[("orchestrator", None)]
        assert hit_test(layout, x, y) == ("orchestrator", None)
        handle_mouse("left", x, y, layout, case.store, case.config, state=state)
        assert calls[-1][1:] == ["terminal", "switch", "--terminal", "term_orch"]
    assert len(calls) == 3 and case.gh_calls == []


def test_lane_view_d76_big_squirrel_without_recorded_terminal_does_nothing(tmp_path, monkeypatch):
    from foragent.watch import handle_mouse, hit_test

    case = lane_d76_case(tmp_path, monkeypatch)
    monkeypatch.setenv("ORCA_TERMINAL_HANDLE", "term_planning")
    calls = lane_d76_opener(monkeypatch)
    state, layout = {}, {}
    frame = lane_d76_frame(case, width=56, state=state, layout=layout)
    hits = hit_positions(layout, frame, 56)
    assert ("orchestrator", None) in hits
    x, y = hits[("orchestrator", None)]
    assert hit_test(layout, x, y) == ("orchestrator", None)
    before = deepcopy(state)
    handle_mouse("left", x, y, layout, case.store, case.config, state=state)
    assert calls == [] and case.gh_calls == []
    assert state == before


def test_lane_view_d76_bubble_reads_processing_request_and_stops_showing_it_after_done(
    tmp_path, monkeypatch, capsys,
):
    case = lane_d76_case(tmp_path, monkeypatch)
    request = add_request(case, capsys, "D75")
    text = "기획 세션 요청: D75"
    assert text not in plain_frame(lane_d76_frame(case))
    assert inbox_command(case, "request-status", str(request["id"]), "processing") == 0
    frame = plain_frame(lane_d76_frame(case))
    assert text in frame, "the bubble must read the processing request from the durable box"
    assert frame.count(text) == 1
    assert any(text in line and "▀" in line for line in frame.splitlines()), (
        "at 160 columns the request bubble must sit beside the large squirrel"
    )
    assert inbox_command(case, "request-status", str(request["id"]), "done") == 0
    assert text not in plain_frame(lane_d76_frame(case))
    assert case.gh_calls == []


def test_lane_view_d76_request_line_counts_processing_and_done_and_is_absent_for_empty_box(
    tmp_path, monkeypatch, capsys,
):
    case = lane_d76_case(tmp_path, monkeypatch)
    assert "요청 " not in plain_frame(lane_d76_frame(case))
    for index, status in enumerate(("processing", "done", "new", "processing")):
        row = add_request(case, capsys, f"요청 {index}")
        if status != "new":
            assert inbox_command(case, "request-status", str(row["id"]), status) == 0
    frame = plain_frame(lane_d76_frame(case))
    line = "요청 2개 처리 중 · 1개 끝"
    assert line in frame and frame.count(line) == 1
    assert case.gh_calls == []
