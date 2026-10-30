"""D77 acceptance: observed main-test quality. Locked by Foragent; do not edit.

Public contract: scorecard --record-main-tests accepts an observation export
with coverage intervals and check results. Scorecard JSON adds main_tests to
each existing window: count (null when unobserved), coverage and failures.
These are user-facing import/output fields, not a prescribed storage format.
"""

from datetime import timedelta
import json

import pytest

from test_local_truth import local_cli, record_gh
from test_orch_scorecard import NOW, completed_run, scorecard_case
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401


@pytest.fixture
def quality(git_history, tmp_path, monkeypatch, isolated_home):
    from foragent.state import StateStore

    case = scorecard_case(git_history, tmp_path, monkeypatch)
    case.main = local_cli(case, monkeypatch)
    monkeypatch.setattr(StateStore, "for_repo", classmethod(
        lambda cls, repo, **kwargs: cls(case.store.root, now=lambda: case.clock[0])))
    case.gh_calls = record_gh(monkeypatch)
    yield case
    assert case.gh_calls == [], "Recording and reading supplied observations must stay offline"


def call(case, capsys, *args):
    code = cli_exit(case.main, ["orch", "scorecard", *map(str, args)])
    output = capsys.readouterr()
    assert code == 0, f"Scorecard must support these public controls: {output.out}{output.err}"
    return output.out


def card(case, capsys, *args):
    return json.loads(call(case, capsys, "--json", *args))


def metric(payload, window):
    shown = payload["window"][window]
    assert "main_tests" in shown, "The existing scorecard must expose observed main-test quality"
    result = shown["main_tests"]
    assert {"count", "coverage", "failures"} <= result.keys(), result
    return result


def observation(identity, *, day=0, result="failure", branch="main"):
    return {"change": f"{day + 1:040x}", "check": f"https://checks.example/{identity}",
            "result": result, "at": (NOW - timedelta(days=day, hours=2)).isoformat(),
            "branch": branch}


def record(case, capsys, observations, *, days=7, complete=True):
    export = case.root / "main-test-observations.json"
    export.write_text(json.dumps({
        "since": (NOW - timedelta(days=days)).isoformat(), "until": NOW.isoformat(),
        "complete": complete, "observations": observations,
    }), encoding="utf-8")
    call(case, capsys, "--record-main-tests", export)


def test_main_failures_compare_with_existing_daily_windows_and_keep_evidence(quality, capsys):
    case = quality
    completed_run(case, 40, finished=NOW - timedelta(hours=3), outcome="merged")
    case.clock[0] = NOW
    current = [observation(f"today-{n}") for n in range(3)]
    history = [observation(f"day-{day}-{n}", day=day)
               for day, count in enumerate((0, 1, 1, 2, 2, 4), 1) for n in range(count)]
    record(case, capsys, current + history + [
        observation("passed", result="success"), observation("pending", result="pending"),
        observation("feature-failed", branch="feature"), observation("too-old", day=8),
    ])
    payload = card(case, capsys)
    today, previous = metric(payload, "today"), metric(payload, "previous")
    assert today["count"] == 3 and today["coverage"] == "complete", today
    assert previous["count"] == 1.5 and previous["coverage"] == "complete", previous
    assert "main_tests" in payload["worse"], payload
    assert {"runs", "merge_rate", "median_minutes_to_merge", "review_returns"} <= payload["window"]["today"]
    assert payload["window"]["today"]["runs"] == 1
    assert len(today["failures"]) == 3
    for expected in current:
        matches = [row for row in today["failures"] if row.get("check") == expected["check"]]
        assert len(matches) == 1, today
        assert all(matches[0].get(key) == expected[key] for key in ("change", "result", "at")), matches
        assert not matches[0].get("caused_by"), "Observation alone cannot assign a culprit merge"
    assert metric(card(case, capsys, "--days", 3), "previous")["count"] == 0.5


def test_observing_the_same_result_twice_never_counts_an_extra_failure(quality, capsys):
    case = quality
    failed = observation("same-result")
    record(case, capsys, [failed, failed])
    first = metric(card(case, capsys), "today")
    assert first["count"] == 1 and len(first["failures"]) == 1, first
    record(case, capsys, [failed])
    assert metric(card(case, capsys), "today") == first
    record(case, capsys, [observation("another-result")])
    assert metric(card(case, capsys), "today")["count"] == 2


def test_no_observations_are_unavailable_in_json_and_human_output(quality, capsys):
    payload = card(quality, capsys)
    for window in ("today", "previous"):
        shown = metric(payload, window)
        assert shown["count"] is None and shown["coverage"] == "unavailable", shown
        assert shown["failures"] == []
    output = call(quality, capsys).lower()
    assert "main" in output and "unavailable" in output, output


def test_complete_successful_observations_are_a_known_zero(quality, capsys):
    record(quality, capsys, [observation("observed-pass", result="success")])
    shown = metric(card(quality, capsys), "today")
    assert shown["count"] == 0 and shown["coverage"] == "complete", shown
    assert shown["failures"] == []


@pytest.mark.parametrize("rows,count", [([], None), ([observation("partial-failure")], 1)],
                         ids=["no-observed-failures", "one-observed-failure"])
def test_incomplete_observations_show_the_limit_instead_of_a_clean_zero(quality, capsys, rows, count):
    record(quality, capsys, rows, days=1, complete=False)
    payload = card(quality, capsys)
    today = metric(payload, "today")
    assert today["count"] == count and today["coverage"] == "partial", today
    assert metric(payload, "previous")["count"] is None
    output = call(quality, capsys).lower()
    assert "main" in output and "partial" in output, output


def test_recorded_quality_remains_readable_after_the_import_file_is_removed(quality, capsys):
    failed = observation("durable-result")
    record(quality, capsys, [failed])
    (quality.root / "main-test-observations.json").unlink()
    # Every CLI read uses persisted local records; the input is no longer available.
    for _ in range(2):
        shown = metric(card(quality, capsys), "today")
        assert shown["count"] == 1 and shown["failures"][0]["check"] == failed["check"]
    output = call(quality, capsys).lower()
    assert "main" in output and "1" in output, output
