"""Emergency stop preserves a running run's real bytes and holds every lane.

All Git and process dependencies use existing offline adapters. No real signal,
git commit, git push, git reset, git checkout or git stash is executed.
"""

from dataclasses import replace

import pytest

from conftest import git_command
from foragent.runner import run_issue
from foragent.scheduler import tick
from test_orch_review_handoff import bind_cli, make_case, tick_context
from test_one_entrance_seat import invoke
from test_skills_autosync import isolated_home  # noqa: F401


@pytest.mark.parametrize("control", ["bulk", "entrance"])
def test_stop_keeps_running_work_and_leaves_all_lanes_stopped(
    git_history, tmp_path, monkeypatch, capsys, isolated_home, control,
):
    lanes = {"api": {"paths": ["api/**"], "agent": "fa-api"},
             "view": {"paths": ["view/**"], "agent": "fa-view"}}
    case = make_case(git_history, tmp_path, monkeypatch, lanes=lanes, skill_libraries=[], test_command="true")
    case.ctx.config = replace(case.ctx.config, lanes=lanes)
    case.tracker.add_labels(1, ["lane:api"])
    second_body = case.tracker.get_issue(1).body
    case.tracker.add_issue(2, "Other lane work", second_body, ["approved", "ready", "lane:view"])
    bind_cli(case, monkeypatch)
    monkeypatch.delenv("FORAGENT_SESSION_ID", raising=False)  # The no-PM emergency entrance.
    monkeypatch.setenv("FORAGENT_ORCH_SPAWN", "off")
    kept = "Unfinished customer work: keep these exact bytes.\n"
    stop_results = []

    def running(kwargs):
        (kwargs["worktree"] / "feature.txt").write_text(kept)
        argv = ("stop",) if control == "entrance" else ("orch", "stop", "--all", "--keep-work", "--why", "Jin requested a stop")
        result = invoke(capsys, *argv)
        stop_results.append(result)
        return "Checkpoint saved; stopping the run."

    case.implementer.steps = [running]
    outcome = run_issue(1, case.ctx)
    assert len(stop_results) == 1, "The running executor must reach the emergency stop control"
    assert stop_results[0].code == 0, "One stop must reach FA's save-and-stop path: " + stop_results[0].text
    assert outcome != "merged", "Emergency stop must not finish by merging the work"
    assert case.store.get_run(1).status == "finished"
    saved = git_command(case.root, "show", "refs/heads/foragent/issue-1:feature.txt")
    assert saved == kept.rstrip("\n"), "Stopping must retain the unfinished work on its recoverable branch"
    launched = []
    ctx = tick_context(case)
    ctx.launcher = lambda n: launched.append(n) or 99000 + n
    tick(ctx)
    tick(ctx)
    assert launched == [], "Both the stopped lane and the other approved lane must stay stopped"
    assert not case.store.active_runs()
    assert case.tracker.find_pr("foragent/issue-1") is None, "A stop is not a publication or merge"
