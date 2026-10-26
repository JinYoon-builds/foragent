"""Acceptance test for cross-lane-start. Locked by Foragent; do not edit."""

from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from conftest import ACCEPTANCE
from foragent import cli, orch_queue, overlap_files, scheduler
from foragent.capacity import write_state
from foragent.issue import body_hash
from foragent.state import StateStore
from test_scheduler import BODY, approve, ctx  # noqa: F401
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401
from test_state_split import offline_acceptance  # noqa: F401


@pytest.fixture(autouse=True)
def offline_cross_lane_start(tmp_path, monkeypatch, isolated_home, offline_acceptance):
    """Only fixture-local Git may spawn; process observations come from ctx's fake table."""
    popen = subprocess.Popen

    def local_git(argv, *args, **kwargs):
        assert isinstance(argv, (list, tuple)) and argv[0] == "git", argv
        assert Path(kwargs["cwd"]).resolve().is_relative_to(tmp_path.resolve()), kwargs
        return popen(argv, *args, **kwargs)

    def no_signal(*args, **kwargs):
        pytest.fail("Cross-lane start tests must never signal real processes")

    monkeypatch.setattr(subprocess, "Popen", local_git)
    monkeypatch.setattr(os, "kill", no_signal)
    monkeypatch.setattr(os, "killpg", no_signal)


@pytest.fixture
def cross_lane_case(ctx, monkeypatch):
    ctx.config = replace(
        ctx.config, repo=ctx.repo, max_parallel=None, overlap_per_file=2,
        skill_libraries=[], test_command="true",
        lanes={lane: {"paths": ["src/foragent/**"]} for lane in ("orch", "view")},
    )
    ctx.start_runs = True
    ctx.launched = []

    def launch(number):
        ctx.launched.append(number)
        return 7000 + number  # The shared ctx fixture records fake birth evidence.

    ctx.launcher = launch
    monkeypatch.setattr(cli, "load_config", lambda root: ctx.config)
    # Every invocation reopens disk state, including after the state directory moves.
    monkeypatch.setattr(StateStore, "for_repo", classmethod(
        lambda cls, repo, **kwargs: cls(ctx.store.root, now=ctx.now)))
    return ctx


def _issue(context, number, files, *, lane="orch", running=False, effective=None):
    body = BODY + "\n## Files\n" + "".join(f"- {path}\n" for path in files)
    approve(context, number, body=body,
            labels=[f"lane:{lane}", "running" if running else "ready"])
    context.store.save_approval(
        number, body=body, body_sha=body_hash(body),
        acceptance_blobs=context.git.blob_shas("origin/main", [ACCEPTANCE]),
        approved_by="human", approved_at=context.now().isoformat(),
    )
    if running:
        context.store.start_run(
            number, branch=f"foragent/issue-{number}",
            worktree=context.store.worktree_path(number), lane=lane, pid=7000 + number,
        )
        if effective is not None:
            # Seed an existing observation, without a worktree or a real commit.
            context.store.update_run(
                number, declared_files=files, changed_files=effective,
                effective_files=effective, files_committed=True,
            )


def _queue(context, capsys, *arguments):
    argv = ["orch", "queue", *map(str, arguments), "--repo-root", str(context.repo_root)]
    code = cli_exit(cli.main, argv)
    captured = capsys.readouterr()
    assert code == 0, f"fa {' '.join(argv)} failed (exit={code}):\n{captured.err}"
    return captured.out


def _order(context, capsys, *scope):
    # Keep the existing queue JSON's order field; no global storage schema is prescribed.
    return json.loads(_queue(context, capsys, *scope, "--json"))["order"]


def _tick_starts(context, expected, *, running=()):
    report = scheduler.tick(context)
    assert report.skipped is None, report
    assert context.launched == expected, report.actions
    assert {run.number for run in context.store.active_runs()} == {*running, *expected}
    assert all(context.store.get_run(n).pid == 7000 + n for n in expected)
    assert all(7000 + n in context.process_table.live for n in (*running, *expected))
    assert context.process_table.signals == []


def test_cross_lane_start_ranks_overlap_count_against_effective_files_before_lane_queue(cross_lane_case):
    case = cross_lane_case
    write_state(case.store, 3, case.now())  # One running run leaves exactly two starts.
    busy_a, busy_b, released = [f"src/foragent/{name}.py" for name in ("busy_a", "busy_b", "released")]
    _issue(case, 90, [busy_a, busy_b, released], running=True, effective=[busy_a, busy_b])
    _issue(case, 40, [busy_a, busy_b])
    _issue(case, 30, [busy_a])
    _issue(case, 70, [released], lane="view")
    orch_queue.set_order(case.store, "orch", [40, 30])
    assert scheduler.lane_order("orch") < scheduler.lane_order("view")
    assert all(overlap_files.can_start(case, lane, number)
               for lane, number in (("orch", 40), ("orch", 30), ("view", 70)))

    _tick_starts(case, [70, 30], running=[90])

    assert case.store.get_run(40) is None
    assert "ready" in case.tracker.get_issue(40).labels
    assert "running" not in case.tracker.get_issue(40).labels


def test_cross_lane_start_uses_global_list_then_lane_queue_position_then_issue_number(cross_lane_case, capsys):
    case = cross_lane_case
    write_state(case.store, 7, case.now())  # Six slots for seven ready candidates.
    files = {n: f"src/foragent/task_{n}.py" for n in (5, 45, 35, 25, 85, 15)}
    _issue(case, 90, list(files.values()), running=True)
    for lane, numbers in (("orch", [5, 45, 35]), ("view", [25, 85, 15])):
        for number in numbers:
            _issue(case, number, [files[number]], lane=lane)
        orch_queue.set_order(case.store, lane, numbers)
    _issue(case, 95, ["src/foragent/clear.py"], lane="view")
    _queue(case, capsys, "--global", 85, 45)
    # Zero overlap beats global priority. At equal overlap, listed order beats
    # both lanes' heads; position zero beats position two; 15 wins the last tie.
    _tick_starts(case, [95, 85, 45, 5, 25, 15], running=[90])

    assert case.store.get_run(35) is None
    assert "ready" in case.tracker.get_issue(35).labels
    assert _order(case, capsys, "--global") == [85, 45]


def test_cross_lane_start_global_cli_persists_shows_and_clears_in_state_dir(
    cross_lane_case, tmp_path, capsys,
):
    case = cross_lane_case
    _queue(case, capsys, "orch", 7, 3)
    _queue(case, capsys, "--global", 85, 45)
    shown = _queue(case, capsys, "--global")
    assert "85" in shown and "45" in shown, shown
    assert shown.index("85") < shown.index("45"), shown
    assert _order(case, capsys, "--global") == [85, 45]
    _queue(case, capsys, "--global", 45, 85)
    assert _order(case, capsys, "--global") == [45, 85]

    restored = tmp_path / "restored-state"
    shutil.copytree(case.store.root, restored)
    _queue(case, capsys, "--global", "--clear")
    assert _order(case, capsys, "--global") == []
    # Copying only the state directory must recover its own priority list.
    case.store = StateStore(restored, now=case.now)
    assert _order(case, capsys, "--global") == [45, 85]
    _queue(case, capsys, "--global", "--clear")
    assert _order(case, capsys, "--global") == []
    assert _order(case, capsys, "orch") == [7, 3]

    # Clearing global preferences preserves today's single-lane order.
    case.config = replace(case.config, lanes={"orch": case.config.lanes["orch"]})
    write_state(case.store, 3, case.now())
    for number in (3, 5, 7):
        _issue(case, number, [f"src/foragent/task_{number}.py"])
    _tick_starts(case, [7, 3, 5])


def test_cross_lane_start_blocked_global_heads_yield_to_the_next_startable_candidate(cross_lane_case, capsys):
    case = cross_lane_case
    write_state(case.store, 3, case.now())  # Two running runs leave only one free slot.
    busy = "src/foragent/busy.py"
    orch_file, view_file = "src/foragent/orch_free.py", "src/foragent/view_free.py"
    _issue(case, 90, [busy, orch_file, view_file], running=True)
    _issue(case, 91, [busy], lane="view", running=True)
    _issue(case, 10, ["src/foragent/waits_for_blocker.py"])
    case.tracker.add_issue(900, "Unfinished prerequisite")
    case.tracker.add_blocked_by(10, 900)
    _issue(case, 20, [busy])
    _issue(case, 5, [orch_file])
    _issue(case, 80, [view_file], lane="view")
    # Both candidates overlap one file, but 20's file is already at the cap.
    assert not overlap_files.can_start(case, "orch", 20)
    assert overlap_files.can_start(case, "view", 80)
    _queue(case, capsys, "--global", 10, 20, 80)

    _tick_starts(case, [80], running=[90, 91])

    assert all(case.store.get_run(number) is None for number in (5, 10, 20))
    assert all("running" not in case.tracker.get_issue(number).labels for number in (5, 10, 20))
    assert "ready" in case.tracker.get_issue(20).labels
    assert case.tracker.get_issue(900).state == "open"
    assert _order(case, capsys, "--global") == [10, 20, 80]
