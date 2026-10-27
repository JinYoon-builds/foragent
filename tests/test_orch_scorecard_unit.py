"""Edges of `fa orch scorecard` the acceptance test does not pin."""

import argparse
from datetime import UTC, datetime, timedelta
import json

import pytest

from foragent import orch_scorecard
from foragent.state import StateStore

NOW = datetime(2026, 10, 23, 12, tzinfo=UTC)


@pytest.fixture
def store(tmp_path):
    clock = [NOW]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    store.clock = clock
    return store


def finish(store, number, *, at, minutes=10, outcome="merged", detail="", merged_at=None):
    store.clock[0] = at - timedelta(minutes=minutes)
    store.start_run(number, branch=f"foragent/issue-{number}", worktree=store.worktree_path(number))
    store.clock[0] = at
    if merged_at is not None:
        store.record_merge(number, merged_at)
    store.finish_run(number, outcome=outcome, detail=detail)
    store.clock[0] = NOW


def test_bucket_edges_are_newer_inclusive_and_older_exclusive():
    assert orch_scorecard._bucket(NOW, NOW, 7) == 0
    assert orch_scorecard._bucket(NOW - timedelta(hours=24), NOW, 7) == 1
    assert orch_scorecard._bucket(NOW - timedelta(hours=23, minutes=59), NOW, 7) == 0
    assert orch_scorecard._bucket(NOW - timedelta(days=7), NOW, 7) is None
    assert orch_scorecard._bucket(NOW + timedelta(seconds=1), NOW, 7) is None


def test_pr_open_counts_as_merged_only_after_a_merge_and_otherwise_is_no_failure(store):
    finish(store, 1, at=NOW - timedelta(hours=2), outcome="pr-open", minutes=20,
           merged_at=NOW - timedelta(hours=1))
    finish(store, 2, at=NOW - timedelta(hours=2), outcome="pr-open")
    card = orch_scorecard.collect(store)
    today = card["window"]["today"]
    assert today["runs"] == 2 and today["merge_rate"] == 0.5
    assert today["median_minutes_to_merge"] == 20  # Start to finish, not to merge.
    assert card["unexplained_failures"] == [] and card["repeated_failures"] == []


def test_github_calls_and_review_returns_land_in_their_windows(store):
    for hour in (NOW - timedelta(hours=1), NOW - timedelta(hours=1), NOW - timedelta(hours=30)):
        store.count_github_call(hour)
    with (store.root / "orch-events.jsonl").open("a") as stream:
        for verdict in ("PASS", "FAIL", "UNUSABLE"):
            stream.write(json.dumps({"at": (NOW - timedelta(hours=3)).isoformat(), "issue": 1,
                                     "kind": "review", "verdict": verdict}) + "\n")
    card = orch_scorecard.collect(store, days=2)
    assert card["window"]["today"]["github_calls_per_hour"] == 2 / 24
    assert card["window"]["previous"]["github_calls_per_hour"] == 1 / 24
    assert card["window"]["today"]["review_returns"] == 2
    assert card["window"]["previous"]["review_returns"] == 0
    assert set(card["worse"]) == {"github_calls_per_hour", "review_returns"}


def test_integer_medians_round_half_up(store):
    # Previous buckets hold 1 and 2 runs: median 1.5 rounds to 2.
    finish(store, 1, at=NOW - timedelta(days=1, hours=1))
    finish(store, 2, at=NOW - timedelta(days=2, hours=1))
    finish(store, 3, at=NOW - timedelta(days=2, hours=2))
    assert orch_scorecard.collect(store, days=3)["window"]["previous"]["runs"] == 2


def test_human_output_names_each_worse_metric_and_failures(store):
    finish(store, 1, at=NOW - timedelta(hours=1), outcome="checks-failed", detail="Boom")
    finish(store, 2, at=NOW - timedelta(hours=1), outcome="checks-failed", detail=" Boom ")
    finish(store, 3, at=NOW - timedelta(hours=1), outcome="crashed")
    card = orch_scorecard.collect(store)
    assert card["repeated_failures"] == [{"reason": "Boom", "count": 2, "issues": [1, 2]}]
    assert card["unexplained_failures"] == [3]
    lines = orch_scorecard.human(card, "en").splitlines()
    assert len(lines) == 5 and "Boom" in lines[3] and "#3" in lines[4]


@pytest.mark.parametrize("value", ["1", "0", "x", "2.5"])
def test_days_below_two_or_not_an_integer_is_a_usage_error(value):
    with pytest.raises(argparse.ArgumentTypeError):
        orch_scorecard._days(value)
