"""Acceptance test for orch-scorecard. Locked by Foragent; do not edit."""

from datetime import UTC, datetime, timedelta
import json
import os
import re

import pytest

from test_local_truth import local_cli, record_gh
from test_orch_review_handoff import make_case
from test_skills_autosync import cli_exit, isolated_home


NOW = datetime(2026, 10, 23, 12, tzinfo=UTC)
METRICS = {"runs", "merge_rate", "median_minutes_to_merge", "github_calls_per_hour",
           "review_returns", "reverts_after_merge", "spec_questions", "phase_minutes", "load"}


def scorecard_case(git_history, tmp_path, monkeypatch):
    from foragent.state import StateStore

    case = make_case(git_history, tmp_path, monkeypatch, language="en",
                     skill_libraries=[], test_command="true")
    # make_case supplies an active run; scorecard seeds start from an empty store.
    case.clock[0] = NOW
    case.store = StateStore(tmp_path / "scorecard-state", now=lambda: case.clock[0])
    case.ctx.store = case.store
    return case


def completed_run(case, number, *, finished, minutes=10, outcome="merged", detail=""):
    case.clock[0] = finished - timedelta(minutes=minutes)
    case.store.start_run(number, branch=f"foragent/issue-{number}",
                         worktree=case.store.worktree_path(number))
    case.clock[0] = finished
    # Today's Run schema has last_progress_at but no finished_at. Keep both the
    # final progress and the clock at completion so either timestamp can be used.
    case.store.update_run(number, last_progress_at=finished.isoformat())
    case.store.finish_run(number, outcome=outcome, detail=detail)


def seed_slow_today(case):
    # Daily medians are 10 runs, 0.7 merged, and 10 minutes. Unequal daily
    # volumes make pooling all historical runs produce different answers.
    number = 100
    for day, runs, merged, minutes in ((1, 10, 7, 10), (2, 10, 7, 10),
                                      (3, 2, 1, 5), (4, 4, 2, 8),
                                      (5, 12, 9, 12), (6, 30, 27, 80)):
        for index in range(runs):
            completed_run(case, number, finished=NOW - timedelta(days=day, hours=6),
                          minutes=minutes, outcome="merged" if index < merged else "checks-failed",
                          detail="" if index < merged else f"Historical failure {number}")
            number += 1
    for index, hours in enumerate((23, 12, 6, 1)):
        completed_run(case, 10 + index, finished=NOW - timedelta(hours=hours), minutes=36,
                      outcome="merged" if index == 0 else "spec-question" if index == 1 else "checks-failed",
                      detail="" if index == 0 else f"Today's failure {index}")
    completed_run(case, 999, finished=NOW - timedelta(days=8), minutes=500)
    case.clock[0] = NOW
    case.store.start_run(888, branch="foragent/issue-888", worktree=case.store.worktree_path(888))


def scorecard(main, capsys, *options):
    argv = ["orch", "scorecard", "--json", *options]
    code = cli_exit(main, argv)
    output = capsys.readouterr()
    assert code == 0, f"fa {' '.join(argv)} exited {code}: {output.err.strip()}"
    payload = json.loads(output.out)
    assert {"window", "worse", "repeated_failures", "unexplained_failures"} <= payload.keys()
    assert {"today", "previous"} <= payload["window"].keys()
    for metrics in payload["window"].values():
        assert METRICS <= metrics.keys()
        assert type(metrics["runs"]) is int and metrics["runs"] >= 0
        assert metrics["merge_rate"] is None or 0 <= metrics["merge_rate"] <= 1
        assert metrics["median_minutes_to_merge"] is None or metrics["median_minutes_to_merge"] >= 0
        assert metrics["github_calls_per_hour"] >= 0
        assert type(metrics["review_returns"]) is int and metrics["review_returns"] >= 0
        assert type(metrics["reverts_after_merge"]) is int and metrics["reverts_after_merge"] >= 0
        assert type(metrics["spec_questions"]) is int and metrics["spec_questions"] >= 0
        assert isinstance(metrics["phase_minutes"], dict)
        assert all(isinstance(phase, str) and type(minutes) in (int, float) and minutes >= 0
                   for phase, minutes in metrics["phase_minutes"].items())
        assert metrics["load"] is None or (type(metrics["load"]) in (int, float) and metrics["load"] >= 0)
    return payload


def test_orch_scorecard_compares_rolling_today_with_daily_medians_and_honors_days(
    git_history, tmp_path, monkeypatch, capsys,
):
    case = scorecard_case(git_history, tmp_path, monkeypatch)
    seed_slow_today(case)
    main = local_cli(case, monkeypatch)
    calls = record_gh(monkeypatch)
    payload = scorecard(main, capsys)
    today, previous = payload["window"]["today"], payload["window"]["previous"]
    # Today: 4 finished runs, 1/4 merged, median([36]) = 36 minutes, 1 question.
    # Prior six days: sorted runs [2,4,10,10,12,30] -> (10+10)/2 = 10;
    # merge rates [.5,.5,.7,.7,.75,.9] -> (.7+.7)/2 = .7;
    # merge minutes [5,8,10,10,12,80] -> (10+10)/2 = 10; questions all 0.
    # No GitHub calls, review returns or reverts in either window -> all 0.
    # --days 5 uses four prior days: runs [2,4,10,10] -> 7;
    # rates [.5,.5,.7,.7] -> .6; minutes [5,8,10,10] -> 9; questions -> 0.
    # Only merge rate (.25 < .7), merge time (36 > 10), questions (1 > 0) worsen.
    # These seeds never enter a phase: phase_minutes={} and load=None in both windows.
    assert today == {"runs": 4, "merge_rate": 0.25, "median_minutes_to_merge": 36,
                     "github_calls_per_hour": 0, "review_returns": 0, "reverts_after_merge": 0,
                     "spec_questions": 1, "phase_minutes": {}, "load": None}
    assert previous == {"runs": 10, "merge_rate": 0.7, "median_minutes_to_merge": 10,
                        "github_calls_per_hour": 0, "review_returns": 0, "reverts_after_merge": 0,
                        "spec_questions": 0, "phase_minutes": {}, "load": None}
    assert set(payload["worse"]) == {"merge_rate", "median_minutes_to_merge", "spec_questions"}
    assert payload["repeated_failures"] == [] and payload["unexplained_failures"] == []

    shorter = scorecard(main, capsys, "--days", "5")
    assert shorter["window"]["today"] == today
    assert shorter["window"]["previous"]["runs"] == 7
    assert shorter["window"]["previous"]["merge_rate"] == pytest.approx(0.6)
    assert shorter["window"]["previous"]["median_minutes_to_merge"] == 9
    assert shorter["window"]["previous"]["spec_questions"] == 0
    assert calls == []


def test_orch_scorecard_groups_repeated_failures_and_exposes_missing_reasons(
    git_history, tmp_path, monkeypatch, capsys,
):
    case = scorecard_case(git_history, tmp_path, monkeypatch)
    reason = "Acceptance baseline timed out"
    for number, detail, outcome in ((20, reason, "checks-failed"), (21, reason, "checks-failed"),
                                     (22, reason, "checks-failed"), (23, "Executor crashed", "error"),
                                     (24, "", "checks-failed"), (25, reason, "merged")):
        completed_run(case, number, finished=NOW - timedelta(hours=2), outcome=outcome, detail=detail)
    completed_run(case, 99, finished=NOW - timedelta(days=2), outcome="checks-failed", detail=reason)
    case.clock[0] = NOW
    main = local_cli(case, monkeypatch)
    calls = record_gh(monkeypatch)
    payload = scorecard(main, capsys)
    assert len(payload["repeated_failures"]) == 1
    repeated, = payload["repeated_failures"]
    assert repeated["reason"] == reason and repeated["count"] == 3
    assert sorted(repeated["issues"]) == [20, 21, 22]
    assert payload["unexplained_failures"] == [24]
    assert payload["window"]["today"]["runs"] == 6
    assert calls == []


def test_orch_scorecard_empty_store_is_null_safe_and_human_output_says_no_change(
    git_history, tmp_path, monkeypatch, capsys,
):
    case = scorecard_case(git_history, tmp_path, monkeypatch)
    main = local_cli(case, monkeypatch)
    calls = record_gh(monkeypatch)
    payload = scorecard(main, capsys)
    for metrics in payload["window"].values():
        assert metrics["runs"] == 0 and metrics["merge_rate"] in (0, None)
        assert metrics["median_minutes_to_merge"] is None
        assert metrics["github_calls_per_hour"] == 0
        assert metrics["review_returns"] == metrics["reverts_after_merge"] == metrics["spec_questions"] == 0
        assert metrics["phase_minutes"] == {} and metrics["load"] is None
    assert payload["worse"] == payload["repeated_failures"] == payload["unexplained_failures"] == []
    assert cli_exit(main, ["orch", "scorecard"]) == 0
    assert re.search(r"no (?:changes?|regressions?|worse)|unchanged|nothing.{0,30}worse|stable",
                     capsys.readouterr().out, re.I)
    assert calls == []


def test_orch_scorecard_human_output_mentions_the_slower_merge_time(
    git_history, tmp_path, monkeypatch, capsys,
):
    case = scorecard_case(git_history, tmp_path, monkeypatch)
    seed_slow_today(case)
    main = local_cli(case, monkeypatch)
    calls = record_gh(monkeypatch)
    assert cli_exit(main, ["orch", "scorecard"]) == 0, "fa orch scorecard must support human output"
    assert "36" in capsys.readouterr().out
    assert calls == []


def test_orch_scorecard_phase_event_medians_include_load_and_flag_slower_checking(
    git_history, tmp_path, monkeypatch, capsys,
):
    case = scorecard_case(git_history, tmp_path, monkeypatch)

    def phase_event(number, *, ended, phase, seconds, load):
        # Seed through the same public StateStore path as the runner record test;
        # do not fabricate journal bytes or bypass phase closure at finish_run.
        case.clock[0] = ended - timedelta(seconds=seconds)
        case.store.start_run(number, branch=f"foragent/issue-{number}",
                             worktree=case.store.worktree_path(number))
        case.store.update_run(number, phase=phase, phase_started_at=case.clock[0].isoformat())
        monkeypatch.setattr(os, "getloadavg", lambda: (load, 0.0, 0.0))
        case.clock[0] = ended
        case.store.finish_run(number, outcome="merged")

    # Today: checking [120,480,1080] seconds / 60 -> [2,8,18], median 8;
    # implementing 60/60=1; reviewing 180/60=3 minutes.
    # Loads [2,8,10,12,4] sort to [2,4,8,10,12], median 8.
    today = [("implementing", 60, 2.0), ("checking", 120, 8.0),
             ("checking", 480, 10.0), ("checking", 1080, 12.0), ("reviewing", 180, 4.0)]
    for number, (phase, seconds, load) in enumerate(today, 10):
        phase_event(number, ended=NOW - timedelta(hours=2), phase=phase, seconds=seconds, load=load)

    # Previous six days, pooled events: checking [60,60,180,600,900]/60
    # -> [1,1,3,10,15], median 3; implementing 120/60=2; reviewing 360/60=6.
    # Unequal daily volumes distinguish this from median([1,1,10])=1.
    # Loads [1,2,3,4,5,.5,1.5] sort to [.5,1,1.5,2,3,4,5], median 2.
    previous = [(1, "checking", 60, 1.0), (2, "checking", 60, 2.0),
                (3, "checking", 180, 3.0), (3, "checking", 600, 4.0),
                (3, "checking", 900, 5.0), (1, "implementing", 120, 0.5),
                (2, "reviewing", 360, 1.5)]
    for number, (day, phase, seconds, load) in enumerate(previous, 100):
        phase_event(number, ended=NOW - timedelta(days=day, hours=2),
                    phase=phase, seconds=seconds, load=load)
    phase_event(998, ended=NOW - timedelta(days=8), phase="outside-window", seconds=60000, load=999.0)
    phase_event(999, ended=NOW + timedelta(hours=1), phase="checking", seconds=60000, load=999.0)
    case.clock[0] = NOW
    main = local_cli(case, monkeypatch)
    calls = record_gh(monkeypatch)
    payload = scorecard(main, capsys)
    assert payload["window"]["today"]["phase_minutes"] == {"implementing": 1, "checking": 8, "reviewing": 3}
    assert payload["window"]["previous"]["phase_minutes"] == {"implementing": 2, "checking": 3, "reviewing": 6}
    assert payload["window"]["today"]["load"] == 8.0
    assert payload["window"]["previous"]["load"] == 2.0
    assert "phase_minutes.checking" in payload["worse"]  # 8 > 3; the other two phases improved.
    assert "phase_minutes.implementing" not in payload["worse"]
    assert "phase_minutes.reviewing" not in payload["worse"]
    assert calls == []
