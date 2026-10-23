"""Acceptance test for fa-briefing-scorecard. Locked by Foragent; do not edit."""

from datetime import timedelta
import re

from test_local_truth import local_cli, record_gh
from test_orch_scorecard import NOW, completed_run, scorecard_case
from test_skills_autosync import cli_exit, isolated_home


def seed_briefing_comparison(case, *, today_minutes):
    # Only merge latency changes, so the metric selected for the one-line
    # briefing is unambiguous without inventing a severity-ranking formula.
    for day in range(1, 7):
        completed_run(case, 100 + day, finished=NOW - timedelta(days=day, hours=6), minutes=10)
    completed_run(case, 10, finished=NOW - timedelta(hours=2), minutes=today_minutes)
    case.clock[0] = NOW


def test_fa_briefing_scorecard_shows_one_named_before_to_now_line_when_worse(
    git_history, tmp_path, monkeypatch, capsys,
):
    case = scorecard_case(git_history, tmp_path, monkeypatch)
    seed_briefing_comparison(case, today_minutes=36)
    main = local_cli(case, monkeypatch)
    calls = record_gh(monkeypatch)
    # #119's existing acceptance tests identify `fa brief` as /fa's data command.
    assert cli_exit(main, ["brief", "--repo-root", str(case.root)]) == 0, (
        "The /fa briefing command must render the scorecard")
    lines = capsys.readouterr().out.splitlines()
    comparisons = [line for line in lines if re.search(r"\b10\b.*(?:→|->).*\b36\b", line)]
    assert len(comparisons) == 1, "Show exactly one scorecard comparison: 10 min → 36 min"
    assert re.search(r"slower|merge|minutes?|\bmin\b|latency|time|median", comparisons[0], re.I)
    assert calls == []


def test_fa_briefing_scorecard_omits_the_line_when_nothing_is_worse(
    git_history, tmp_path, monkeypatch, capsys,
):
    case = scorecard_case(git_history, tmp_path, monkeypatch)
    seed_briefing_comparison(case, today_minutes=10)
    main = local_cli(case, monkeypatch)
    calls = record_gh(monkeypatch)
    assert cli_exit(main, ["brief", "--repo-root", str(case.root)]) == 0, (
        "The /fa briefing must also succeed without a scorecard regression")
    output = capsys.readouterr().out
    assert output.strip(), "The normal briefing must still be rendered"
    assert not re.search(r"scorecard|slower|merge_rate|median_minutes_to_merge|github_calls_per_hour|"
                         r"review_returns|reverts_after_merge|no changes?|no regressions?|unchanged", output, re.I)
    assert not re.search(r"\d[^\n]*(?:→|->)[^\n]*\d", output)
    assert calls == []
