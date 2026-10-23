"""Acceptance test for scorecard-records. Locked by Foragent; do not edit."""

from dataclasses import asdict
from datetime import UTC, datetime, timedelta
import json
import re

import pytest

from test_local_truth import local_cli, record_gh
from test_orch_review_handoff import make_case
from test_skills_autosync import isolated_home


def review_events(store):
    path = store.root / "orch-events.jsonl"
    return [event for line in (path.read_text().splitlines() if path.exists() else [])
            if (event := json.loads(line))["kind"] == "review"]


def test_scorecard_records_cli_github_calls_are_persisted_in_utc_hour_buckets(
    git_history, tmp_path, monkeypatch, capsys,
):
    from foragent import cli
    from foragent.state import StateStore

    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true")
    case.store.finish_run(1, outcome="spec-question")
    main = local_cli(case, monkeypatch)
    # Keep the real CLI-built GhTracker; only suppress the background notifier.
    monkeypatch.setattr(cli, "spawn_notify", lambda *args: None)
    calls = record_gh(monkeypatch, reply=lambda argv: "[]")
    expected = {}
    for _ in range(2):
        before = len(calls)
        assert main(["gate", "tick", "--no-start", "--json"]) == 0
        capsys.readouterr()
        assert len(calls) > before, "A fresh gate refresh must exercise the real gh boundary"
        hour = case.clock[0].astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        expected[hour] = len(calls) - before
        case.clock[0] += timedelta(hours=1)

    reader = getattr(StateStore(case.store.root), "github_calls", None)
    assert callable(reader), "StateStore needs a public github_calls() hourly counter API"
    rows = reader()
    actual = {}
    for row in rows:
        assert {"hour", "calls"} <= row.keys()
        hour = datetime.fromisoformat(row["hour"])
        assert hour.utcoffset() == timedelta(0)
        assert hour == hour.replace(minute=0, second=0, microsecond=0)
        assert type(row["calls"]) is int and row["calls"] > 0
        assert hour not in actual, "An hour must have only one counter record"
        actual[hour] = row["calls"]
    assert actual == expected
    assert sum(actual.values()) == len(calls)


@pytest.mark.parametrize("first,verdict", [
    ("VERDICT: FAIL\nfeature.txt:1 loses the selected locale", "FAIL"),
    ("The retry path loses the selected locale.\nSee feature.txt:1.", "UNUSABLE"),
], ids=["fail-then-pass", "unusable-then-pass"])
def test_scorecard_records_each_review_round_is_journaled_before_the_next(
    git_history, tmp_path, monkeypatch, first, verdict,
):
    from foragent.runner import run_issue

    record_gh(monkeypatch)
    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true")
    during_second_review = []

    def accept(kwargs):
        during_second_review.extend(review_events(case.store))
        return "VERDICT: PASS"

    case.reviewer.steps = [first, accept]
    case.implementer.steps = [case.implement, case.implement]
    assert run_issue(1, case.ctx) == "merged"
    assert len(case.reviewer.calls) == 2
    events = [event for event in review_events(case.store) if event["issue"] == 1]
    assert len(events) == 2, "Each review round must write a local kind=review event"
    for event, expected in zip(events, (verdict, "PASS"), strict=True):
        assert re.search(rf"\b{expected}\b", event["summary"])
    assert len(during_second_review) == 1, "The first verdict must be durable before the next review"
    assert re.search(rf"\b{verdict}\b", during_second_review[0]["summary"])


def test_scorecard_records_spec_question_adds_no_review_event_to_an_existing_journal(
    git_history, tmp_path, monkeypatch,
):
    from foragent.runner import run_issue

    record_gh(monkeypatch)
    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true")
    approval = case.store.load_approval(1)
    assert run_issue(1, case.ctx) == "merged"

    case.tracker.add_issue(7, "Choose a locale", approval.body, labels=["approved", "running"])
    case.store.save_approval(**{**asdict(approval), "number": 7})
    case.clock[0] += timedelta(minutes=1)
    case.store.start_run(7, branch="foragent/issue-7", worktree=case.store.worktree_path(7))

    def question(kwargs):
        path = kwargs["worktree"] / ".foragent/QUESTION.md"
        path.parent.mkdir(exist_ok=True)
        path.write_text("Which locale?\nA: English (recommended)\nB: Korean\n")

    case.implementer.steps = [question]
    assert run_issue(7, case.ctx) == "spec-question"
    assert len(case.reviewer.calls) == 1
    events = review_events(case.store)
    assert len(events) == 1, "Keep the completed review of #1 and add none for the spec question"
    assert events[0]["issue"] == 1 and "PASS" in events[0]["summary"]
