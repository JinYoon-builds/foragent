"""Acceptance test for squirrel-running. Locked by Foragent; do not edit."""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import re

import pytest

from test_heartbeats import heartbeats_write
from test_skills_autosync import isolated_home
from test_view_done_state import cell_count, done_snapshot


@pytest.fixture(autouse=True)
def view_offline(isolated_home, tmp_path, monkeypatch):
    """Share existing HOME isolation and forbid external I/O for the new view tests."""
    import curses
    import socket
    import subprocess
    import tempfile
    from pathlib import Path

    from foragent import executors

    def forbidden(*args, **kwargs):
        pytest.fail("lane-view acceptance must not open a terminal, network, or executor")

    monkeypatch.setenv("FORAGENT_ORCH_SPAWN", "off")
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    for name in ("ORCA_TERMINAL_HANDLE", "TMUX"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(curses, "initscr", forbidden)
    monkeypatch.setattr(curses, "wrapper", forbidden)
    monkeypatch.setattr(executors.CodexExecutor, "run", forbidden)
    monkeypatch.setattr(executors.ClaudeExecutor, "run", forbidden)
    popen = subprocess.Popen

    def local_process(argv, *args, **kwargs):
        # Git fixtures and Done when's file checks are the only child processes.
        # In particular, terminal CLIs and gh cannot bypass the Python guards.
        assert isinstance(argv, (list, tuple)) and argv
        assert Path(argv[0]).name in {"git", "sh", "bash"}, argv
        return popen(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", local_process)


def plain_frame(value):
    """Remove only terminal transport escapes, retaining all displayed characters."""
    value = re.sub(r"\x1b\]8;[^\x1b\x07]*(?:\x1b\\|\x07)", "", value)
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", value)


def running_snapshot():
    """Extend the existing view builder with one lane for each factual state."""
    from foragent.labels import lane_display

    snapshot, config = done_snapshot(count=5)
    names = ("work", "queue", "human", "blocked", "finished")
    old_names = list(config.lanes)
    config = replace(config, repo="owner/repo", lanes={
        name: {"title": name, "emoji": "", "paths": [f"{name}/**"]} for name in names})
    rows = {}
    done = {}
    for index, (old, name) in enumerate(zip(old_names, names)):
        row = deepcopy(snapshot.status["lanes"][old])
        row.update(slug=name, lane_display=lane_display(config, name, "ko"))
        if name != "finished":
            number = 101 + index
            item = {"number": number, "title": f"Task {number}"}
            row.update(state=("working", "queued", "waiting-human", "blocked")[index],
                       issue=number, title=item["title"], last_activity_seconds=60,
                       elapsed_seconds=60, phase="implementing" if name == "work" else None,
                       queued=[item] if name == "queue" else [],
                       running=[item] if name == "work" else [])
        rows[name] = row
        done[name] = snapshot.done[old]
    status = {**snapshot.status, "lanes": rows,
              "summary": {"working": 1, "blocked": 1, "waiting_human": 1, "queued": 1},
              "active_runs": [{"number": 101, "lane": "work", "attempts": 1,
                               "last_activity_at": (snapshot.captured_at - timedelta(seconds=60)).isoformat()}]}
    return replace(snapshot, status=status, done=done, total=snapshot.completed + 4), config


def running_view_case(git_history, tmp_path, monkeypatch):
    """Use real run/log/heartbeat records when a live orchestrator is required."""
    from test_view_evidence import view_evidence_log
    from test_view_status import status_case

    case, main = status_case(git_history, tmp_path, monkeypatch, language="ko")
    case.store.update_run(1, pid=88001, phase="implementing",
                          last_activity_at=case.clock[0].isoformat())
    monkeypatch.setattr("foragent.watch.os.kill", lambda pid, signal: None)
    view_evidence_log(case, minutes=1)
    heartbeats_write(case.store, "orch", case.clock[0])
    heartbeats_write(case.store, "gate", case.clock[0])
    case.tracker.add_issue(99, "Current order")
    for number in (1, 2, 3, 4):
        case.tracker.set_parent(number, 99)
    case.tracker.add_issue(41, "Finished task", labels=["lane:docs"], state="closed")
    case.tracker.set_parent(41, 99)
    case.store.start_run(41, branch="foragent/issue-41", worktree=case.store.worktree_path(41), lane="docs")
    case.store.finish_run(41, outcome="merged")
    return case, main


def test_squirrel_running_poses_follow_facts_and_acorns_include_issue_numbers(
    git_history, tmp_path, monkeypatch, capsys,
):
    from foragent.watch import lane_pose
    from foragent import watch, watch_art
    from test_view_evidence import view_evidence_log, view_evidence_snapshot

    case, main = running_view_case(git_history, tmp_path, monkeypatch)
    # A fresh log, rather than a state timestamp, is the working evidence.
    case.store.update_run(1, last_activity_at=(case.clock[0] - timedelta(minutes=12)).isoformat())
    snapshot = view_evidence_snapshot(case, main, capsys)
    config = case.config
    rows = snapshot.status["lanes"]
    for lane, pose in {"api": "gnaw", "web": "sit", "decision": "wave", "docs": "sleep"}.items():
        assert lane_pose(rows[lane], snapshot, snapshot.captured_at) == pose
    assert lane_pose(rows["api"], snapshot, snapshot.captured_at + timedelta(minutes=10)) != "gnaw"
    case.store.pause_lane("web", case.clock[0] + timedelta(hours=1))
    limited = view_evidence_snapshot(case, main, capsys)
    assert lane_pose(limited.status["lanes"]["web"], limited, case.clock[0]) == "sit"
    numbers = []
    acorn = watch_art.acorn

    def record(number, done):
        numbers.append(number)
        return acorn(number, done)

    monkeypatch.setattr(watch_art, "acorn", record)
    frame = watch.render_frame(snapshot, config, case.root, width=100)
    for number in (1, 2, 3, 4, 41):
        assert f"#{number}" in plain_frame(frame) and number in numbers
    lines = plain_frame(frame).splitlines()
    assert any("!" in line and any("▀" in following for following in lines[index + 1:index + 3])
               for index, line in enumerate(lines)), "blocked marker belongs above the squirrel"
    case.store.update_run(1, last_activity_at=case.clock[0].isoformat())
    view_evidence_log(case, minutes=10)
    quiet = view_evidence_snapshot(case, main, capsys)
    assert lane_pose(quiet.status["lanes"]["api"], quiet, case.clock[0]) != "gnaw"


def test_squirrel_running_width_tiers_keep_bubble_beside_then_below_then_text(
    git_history, tmp_path, monkeypatch, capsys,
):
    from foragent.watch import render_frame
    from test_view_evidence import view_evidence_snapshot

    case, main = running_view_case(git_history, tmp_path, monkeypatch)
    snapshot = view_evidence_snapshot(case, main, capsys)
    config = case.config
    snapshot.events = [{"at": snapshot.captured_at.isoformat(), "issue": 101, "kind": "retry",
                        "summary": "internal retry", "say": "다시 맡겼어요"}]
    for width in (120, 56, 55, 44, 43, 32):
        lines = plain_frame(render_frame(snapshot, config, case.root, width=width)).splitlines()
        assert all(cell_count(line) <= width for line in lines), (width, lines)
        bubble = [index for index, line in enumerate(lines) if "다시 맡겼어요" in line]
        assert len(bubble) == 1, (width, lines)
        if width >= 56:
            assert "▀" in lines[bubble[0]], "speech must share the orchestrator's pixel rows"
        elif width >= 44:
            pixels = [index for index, line in enumerate(lines) if "▀" in line]
            assert pixels and bubble[0] > min(pixels)
            assert "▀" not in lines[bubble[0]], "stacked speech is below the orchestrator"
        else:
            assert all("▀" not in line for line in lines)
            for name, lane in config.lanes.items():
                label = lane.get("title") or name
                assert sum(label in line for line in lines) == 1, (width, label, lines)


def test_squirrel_running_bubble_uses_latest_say_and_truncates_to_two_lines(
    git_history, tmp_path, monkeypatch, capsys,
):
    from foragent.watch import render_frame
    from test_view_evidence import view_evidence_snapshot

    case, main = running_view_case(git_history, tmp_path, monkeypatch)
    snapshot = view_evidence_snapshot(case, main, capsys)
    config = case.config
    snapshot.events = [
        {"at": snapshot.captured_at.isoformat(), "issue": 101, "kind": "retry",
         "summary": "raw implementation hint", "say": "최신 발언 " + "긴 설명 " * 60},
        {"at": (snapshot.captured_at - timedelta(minutes=1)).isoformat(), "issue": 104,
         "kind": "retry", "summary": "older internal hint", "say": "예전 발언"},
    ]
    frame = plain_frame(render_frame(snapshot, config, case.root, width=56))
    lines = frame.splitlines()
    start = next((i for i, line in enumerate(lines) if "최신 발언" in line), None)
    assert start is not None, "bubble must display say, not the raw summary"
    bubble = lines[start:start + 2]
    assert any("…" in line for line in bubble)
    assert not any("예전 발언" in line or "raw implementation" in line for line in bubble)
    assert not any("긴 설명" in line for line in lines[start + 2:]), "bubble text is at most two lines"


def test_squirrel_running_animation_defaults_on_and_yaml_can_turn_it_off(tmp_path):
    from foragent.config import Config, load_config
    from foragent.watch import render_frame

    assert Config().animation is True
    snapshot, config = running_snapshot()
    (tmp_path / ".foragent.yml").write_text("animation: false\n", encoding="utf-8")
    assert load_config(tmp_path).animation is False
    config = replace(config, animation=False)
    frame = plain_frame(render_frame(snapshot, config, tmp_path, width=100))
    assert "▀" not in frame
    assert all(name in frame for name in config.lanes)
    assert all(f"#{number}" in frame for number in (101, 102, 103, 104, 41))
    assert f"{snapshot.completed}/{snapshot.total}" in frame
