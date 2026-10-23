"""Acceptance test for scorecard-records. Locked by Foragent; do not edit."""

from dataclasses import asdict
from datetime import UTC, datetime, timedelta
import json
import os
import re

import pytest

from test_local_truth import local_cli, record_gh
from test_orch_review_handoff import make_case
from test_skills_autosync import isolated_home


def review_events(store):
    path = store.root / "orch-events.jsonl"
    return [event for line in (path.read_text().splitlines() if path.exists() else [])
            if (event := json.loads(line))["kind"] == "review"]


def phase_events(store):
    path = store.root / "orch-events.jsonl"
    return [event for line in (path.read_text().splitlines() if path.exists() else [])
            if (event := json.loads(line))["kind"] == "phase"]


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


def test_scorecard_records_phase_changes_journal_elapsed_seconds_and_load_through_finish(
    git_history, tmp_path, monkeypatch,
):
    from foragent import runner
    from foragent.state import StateStore

    calls = record_gh(monkeypatch)
    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true")
    started = case.clock[0]
    monkeypatch.setattr(os, "getloadavg", lambda: (9.25, 6.5, 4.0))
    case.ctx.poll_hook = lambda execute, poll: execute()
    during_review, during_ci, during_merge, checks = [], [], [], []

    def implement(kwargs):
        case.implement(kwargs)
        case.clock[0] += timedelta(seconds=7)

    run_logged = runner.run_logged

    def check(argv, **kwargs):
        # Run the real Done-when commands; only their elapsed clock time is fake.
        result = run_logged(argv, **kwargs)
        checks.append(list(argv))
        case.clock[0] += timedelta(seconds=11)
        return result

    def review(kwargs):
        during_review.extend(phase_events(StateStore(case.store.root)))
        case.clock[0] += timedelta(seconds=13)
        return "VERDICT: PASS"

    pr_checks, merge_pr = case.tracker.pr_checks, case.tracker.merge_pr

    def ci(pr):
        during_ci.extend(phase_events(StateStore(case.store.root)))
        case.clock[0] += timedelta(seconds=17)
        return pr_checks(pr)

    def merge(pr, **kwargs):
        during_merge.extend(phase_events(StateStore(case.store.root)))
        case.clock[0] += timedelta(seconds=19)
        return merge_pr(pr, **kwargs)

    case.implementer.steps = [implement]
    case.reviewer.steps = [review]
    monkeypatch.setattr(runner, "run_logged", check)
    monkeypatch.setattr(case.tracker, "pr_checks", ci)
    monkeypatch.setattr(case.tracker, "merge_pr", merge)
    assert runner.run_issue(1, case.ctx) == "merged", case.store.log_path(1).read_text()
    assert len(checks) == 2  # Two Done-when commands: checking takes 2 * 11 = 22 seconds.
    assert case.clock[0] == started + timedelta(seconds=78)  # 7 + 22 + 13 + 17 + 19.
    store = StateStore(case.store.root)
    assert store.get_run(1).status == "finished"
    events = phase_events(store)
    assert len(events) == 5, "Each ended phase, including the final phase, must append a local kind=phase event"
    assert [(event["issue"], event["phase"], event["seconds"], event["load"]) for event in events] == [
        (1, "implementing", 7, 9.25),
        (1, "checking", 22, 9.25),
        (1, "reviewing", 13, 9.25),
        (1, "ci", 17, 9.25),
        (1, "merging", 19, 9.25),
    ]
    # End timestamps are cumulative: 7, 7+22=29, 29+13=42, 42+17=59, 59+19=78.
    assert [datetime.fromisoformat(event["at"]) for event in events] == [
        started + timedelta(seconds=seconds) for seconds in (7, 29, 42, 59, 78)
    ]
    assert all(type(event["load"]) is float for event in events)
    # Transitions must append durably as they happen, rather than batch at finish.
    assert during_review == events[:2]
    assert during_ci == events[:3]
    assert during_merge == events[:4]
    assert calls == []
