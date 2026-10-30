"""Boundaries of the D77 main-test failure measure (foragent.orch_scorecard_main_tests)."""

from datetime import UTC, datetime, timedelta
import json

import pytest

from foragent import orch_scorecard, orch_scorecard_main_tests as main_tests
from foragent.state import StateStore

NOW = datetime(2026, 10, 23, 12, tzinfo=UTC)


def export(tmp_path, rows, *, since, until=NOW, complete=True, name="export.json"):
    path = tmp_path / name
    path.write_text(json.dumps({"since": since.isoformat(), "until": until.isoformat(),
                                "complete": complete, "observations": rows}), encoding="utf-8")
    return path


def row(check, *, at=NOW - timedelta(hours=1), result="failure", branch="main", change="a" * 40):
    return {"change": change, "check": check, "result": result, "at": at.isoformat(), "branch": branch}


@pytest.fixture
def store(tmp_path):
    return StateStore(tmp_path / "state", now=lambda: NOW)


def test_only_the_configured_base_branch_counts(store, tmp_path):
    rows = [row("on-main", branch="trunk"), row("on-main-word", branch="main")]
    main_tests.record(store, export(tmp_path, rows, since=NOW - timedelta(days=1)), base_branch="trunk")
    today = main_tests.daily(store, NOW, 2)[0]
    assert [failure["check"] for failure in today["failures"]] == ["on-main"]


def test_a_later_result_of_the_same_check_is_a_new_observation_but_a_repeat_is_not(store, tmp_path):
    first = export(tmp_path, [row("c", result="pending"), row("c")], since=NOW - timedelta(days=1))
    assert main_tests.record(store, first) == 2
    assert main_tests.record(store, first) == 0
    assert main_tests.daily(store, NOW, 2)[0]["count"] == 1


def test_a_gap_between_complete_exports_makes_the_day_partial(store, tmp_path):
    main_tests.record(store, export(tmp_path, [], since=NOW - timedelta(hours=24), until=NOW - timedelta(hours=13)))
    main_tests.record(store, export(tmp_path, [], since=NOW - timedelta(hours=12), name="b.json"))
    assert main_tests.daily(store, NOW, 2)[0] == {"count": None, "coverage": "partial", "failures": []}
    main_tests.record(store, export(tmp_path, [], since=NOW - timedelta(hours=14),
                                    until=NOW - timedelta(hours=11), name="c.json"))
    assert main_tests.daily(store, NOW, 2)[0] == {"count": 0, "coverage": "complete", "failures": []}


def test_a_known_zero_before_still_makes_a_failure_today_worse(store, tmp_path):
    main_tests.record(store, export(tmp_path, [row("now")], since=NOW - timedelta(days=2)))
    assert "main_tests" in orch_scorecard.collect(store, days=2)["worse"]


def test_previous_median_uses_only_complete_days_and_reports_partial(store, tmp_path):
    rows = [row("old", at=NOW - timedelta(days=1, hours=1))]
    main_tests.record(store, export(tmp_path, rows, since=NOW - timedelta(days=2)))
    _, before = main_tests.windows(store, NOW, 4)
    assert before["count"] == 1 and before["coverage"] == "partial", before


@pytest.mark.parametrize("payload", [
    "not json", [], {"since": "x", "until": NOW.isoformat(), "complete": True, "observations": []},
    {"since": NOW.isoformat(), "until": NOW.isoformat(), "complete": "yes", "observations": []},
    {"since": NOW.isoformat(), "until": NOW.isoformat(), "complete": True, "observations": [{"check": "c"}]},
])
def test_an_invalid_export_records_nothing(store, tmp_path, payload):
    path = tmp_path / "bad.json"
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    with pytest.raises(main_tests.ExportError):
        main_tests.record(store, path)
    assert not (store.root / main_tests.FILE).exists()


def test_the_worse_main_tests_line_reads_counts(store, tmp_path):
    rows = [row("now-1"), row("now-2"), row("before", at=NOW - timedelta(days=1, hours=1))]
    main_tests.record(store, export(tmp_path, rows, since=NOW - timedelta(days=2)))
    card = orch_scorecard.collect(store, days=2)
    assert "main_tests" in card["worse"]
    assert orch_scorecard.worst_line(card, "en") == "Worse than the days before: main-test failures 1 -> 2"
    shown = orch_scorecard.human(card, "en")
    assert ("Main tests: 2 failures observed in the last 24 h (complete); "
            "daily median before 1 (complete)") in shown
    assert shown.count("main failure:") == 2
