"""Acceptance contract: notify-only-actions (D36 and D71).

Immediate pushes describe new human decisions, one per item. The scheduled
morning digest is the sole exception; counters and autonomous repairs are pull
information. D71 judgment is supplied by the orchestrator, not inferred here.
"""

from dataclasses import replace
from datetime import UTC, datetime
import json
import time
from zoneinfo import ZoneInfo

import pytest

from foragent.inspect_actions import answer, notify
from foragent.inspect_data import InspectContext, collect
from foragent.orch import escalate, retry
from foragent.state import StateStore
from test_local_truth import record_gh
from test_orch_review_handoff import make_case
from test_view_notify import notify_store


def notification_case(git_history, tmp_path, monkeypatch):
    record_gh(monkeypatch)
    case = make_case(git_history, tmp_path, monkeypatch, notify="claude-push", language="en")
    # Reuse the delivery fixture, so real send() writes to this case's outbox.
    delivery_store = notify_store(tmp_path, monkeypatch)
    case.clock[0] = datetime(2026, 10, 23, 13, tzinfo=ZoneInfo("Asia/Seoul")).astimezone(UTC)
    ctx = InspectContext(case.tracker, case.store, "owner/repo", case.config,
                         now=lambda: case.clock[0], repo_root=case.root, git=case.git)
    return case, ctx, delivery_store.root / "push-outbox.jsonl"


def pushes(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def push_text(row):
    return " ".join(str(row.get(key) or "") for key in ("title", "line", "link"))


def cycle(ctx):
    # Exercise both public observation and delivery on every cycle.
    inbox = collect(ctx)
    notify(ctx)
    return inbox


def ask(case, number, question):
    case.tracker.add_issue(number, f"Decision for issue {number}")
    escalate(case.ctx, number, question, "Read the scope and checked the available alternatives.",
             options=["Keep the current scope", "Include the additional work"], recommend=1)


def merged_work(case, number, *, checks=False):
    if number not in {issue.number for issue in case.tracker.list_issues()}:
        case.tracker.add_issue(number, f"Completed work {number}")
    body = f"Closes #{number}\n"
    if checks:
        body += "\n## Needs a human check\n\n- [ ] Choose the rollout audience.\n"
    pr = case.tracker.create_pr(head=f"foragent/issue-{number}", base="main",
                                title=f"Completed work {number}", body=body)
    case.tracker.merge_pr(pr, expected_head_sha=case.tracker.pr_head_sha(pr))
    case.tracker.close_issue(number)
    return pr


def test_new_d71_questions_get_one_push_each_and_stay_deduped_after_restart(
    git_history, tmp_path, monkeypatch,
):
    case, ctx, outbox = notification_case(git_history, tmp_path, monkeypatch)
    questions = {6: "Should this order include billing?", 8: "Should we prioritize export next?"}
    for number, question in questions.items():
        ask(case, number, question)
    for _ in range(3):
        cycle(ctx)
    # A new process must remember which items were already delivered.
    restarted = replace(ctx, store=StateStore(case.store.root, now=ctx.now))
    for _ in range(3):
        cycle(restarted)
    records = pushes(outbox)
    assert len(records) == 2, f"Two new D71 questions require two item pushes; got {records}"
    for number, question in questions.items():
        matching = [row for row in records if f"#{number}" in push_text(row)]
        assert len(matching) == 1 and question in push_text(matching[0]), (
            f"Push for issue #{number} must name the issue and its decision: {records}")
        assert all(f"#{other}" not in push_text(matching[0]) for other in questions if other != number)


def test_new_merge_decision_names_pr_and_does_not_repeat_an_existing_question(
    git_history, tmp_path, monkeypatch,
):
    case, ctx, outbox = notification_case(git_history, tmp_path, monkeypatch)
    ask(case, 6, "Should this order include billing?")
    cycle(ctx)
    before = len(pushes(outbox))
    case.tracker.add_issue(8, "Export preview", labels=["approved"])
    pr = case.tracker.create_pr(head="foragent/issue-8", base="main", title="Export preview",
                                body="Closes #8\n\nReady for a human merge decision.")
    for _ in range(4):
        cycle(ctx)
    records = pushes(outbox)[before:]
    assert len(records) == 1, f"One newly waiting PR requires one push; got {records}"
    message = push_text(records[0])
    assert "#6" not in message, f"An existing question must not be pushed again: {message}"
    assert f"#{pr}" in message or f"/pull/{pr}" in message, f"Push must identify PR #{pr}: {message}"
    assert "Export preview" in message and "merge" in message.lower(), (
        f"Push must ask for the PR merge decision: {message}")


def test_progress_counts_technical_failures_and_autonomous_retries_produce_no_pushes(
    git_history, tmp_path, monkeypatch,
):
    case, ctx, outbox = notification_case(git_history, tmp_path, monkeypatch)
    ask(case, 6, "Should this order include billing?")
    cycle(ctx)
    before = len(pushes(outbox))
    # Try 54 -> 55, attempt 54 -> 55, and changing running/queued counts.
    # Leave the interleaved numbers free for PRs, as on GitHub.
    for number in range(100, 208, 2):
        merged_work(case, number)
    case.store.update_run(1, attempts=54)
    cycle(ctx)
    merged_work(case, 208)
    case.store.update_run(1, attempts=55, transient_errors=1)
    case.tracker.add_issue(2, "Queued work", labels=["approved", "ready"])
    cycle(ctx)
    case.tracker.remove_labels(2, ["ready"])
    case.tracker.add_labels(2, ["running"])
    cycle(ctx)
    # Legacy technical flags are raw evidence, not D71 orchestrator questions.
    for number in (10, 11):
        case.tracker.add_issue(number, f"Technical failure {number}", labels=["needs-human"])
        case.tracker.comment(number,
                             "<!-- foragent:needs-human reason=retries-exceeded -->\n"
                             "Temporary CI transport failure; no human choice is needed.")
        cycle(ctx)
    case.tracker.add_issue(12, "Repairable blocker", labels=["blocked"])
    case.tracker.comment(12, "<!-- foragent:blocked reason=timeout -->\nCI connection timed out.")
    cycle(ctx)
    retry(case.ctx, 12, "Transport recovered; retry with the same scope.")
    for number in (10, 11):
        retry(case.ctx, number, "CI transport is available again.")
        cycle(ctx)
    for _ in range(3):
        cycle(ctx)
    records = pushes(outbox)[before:]
    assert records == [], f"Progress and orchestrator-resolved technical work must stay silent: {records}"


def test_answering_and_closing_one_question_does_not_repush_the_remaining_question(
    git_history, tmp_path, monkeypatch,
):
    case, ctx, outbox = notification_case(git_history, tmp_path, monkeypatch)
    ask(case, 6, "Should this order include billing?")
    ask(case, 8, "Should we prioritize export next?")
    cycle(ctx)
    before = len(pushes(outbox))
    answer(ctx, 6, "Keep the current scope.")
    cycle(ctx)
    case.tracker.close_issue(6)
    for _ in range(4):
        cycle(ctx)
    records = pushes(outbox)[before:]
    assert records == [], f"Resolving #6 must not notify about the unchanged question #8: {records}"


@pytest.mark.parametrize("surface", ["human-check-list", "push"])
def test_closed_issues_with_stale_human_evidence_never_enter_human_checks_or_pushes(
    git_history, tmp_path, monkeypatch, surface,
):
    case, ctx, outbox = notification_case(git_history, tmp_path, monkeypatch)
    for number in (6, 8, 112):
        ask(case, number, f"Which audience should receive issue #{number}?")
        case.store.save_flag(number, "spec-question")
        case.store.start_run(number, branch=f"foragent/issue-{number}",
                             worktree=case.store.worktree_path(number))
        case.store.finish_run(number, outcome="spec-question")
        merged_work(case, number, checks=True)
        assert case.tracker.get_issue(number).state == "closed"
        assert case.store.load_flag(number) is not None, "Keep stale evidence in the regression fixture"
    inbox = cycle(ctx)
    if surface == "human-check-list":
        numbers = [item.number for item in inbox.items if item.group == "human-check"]
        assert numbers == [], f"Closed issues must be absent from Human check; got {numbers}"
        assert inbox.counts["human-check"] == 0
    else:
        for _ in range(3):
            cycle(ctx)
        records = pushes(outbox)
        assert records == [], f"Closed issues with stale human evidence must never push: {records}"


@pytest.fixture(autouse=True)
def local_timezone(monkeypatch):
    # Supply the machine's local timezone as well as an aware UTC clock: 08:00
    # here is 23:00 UTC on the previous date, catching UTC-based daily dedupe.
    try:
        with monkeypatch.context() as patch:
            patch.setenv("TZ", "Asia/Seoul")
            time.tzset()
            yield ZoneInfo("Asia/Seoul")
    finally:
        time.tzset()


def test_default_morning_digest_once_per_local_day_even_after_restarts(
    git_history, tmp_path, monkeypatch, local_timezone,
):
    case, ctx, outbox = notification_case(git_history, tmp_path, monkeypatch)
    case.clock[0] = datetime(2026, 10, 23, 7, 58, tzinfo=local_timezone).astimezone(UTC)
    merged_work(case, 20)
    observed = []
    for day in (23, 24):
        for hour, minute in ((7, 59), (8, 0), (8, 0), (8, 30), (12, 0), (23, 59)):
            case.clock[0] = datetime(2026, 10, day, hour, minute,
                                     tzinfo=local_timezone).astimezone(UTC)
            ctx = replace(ctx, store=StateStore(case.store.root, now=ctx.now))
            cycle(ctx)
            observed.append(len(pushes(outbox)))
    assert observed == [0, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2], (
        f"Default 08:00 local digest must occur once per local date, across restarts; got {observed}")


def test_morning_digest_respects_configured_local_hour(
    git_history, tmp_path, monkeypatch, local_timezone,
):
    from foragent.config import load_config

    case, ctx, outbox = notification_case(git_history, tmp_path, monkeypatch)
    (case.root / ".foragent.yml").write_text(
        "repo: owner/repo\nnotify: claude-push\nnotify_summary_hour: 10\n")
    ctx.config = load_config(case.root, global_path=tmp_path / "absent-global.yml")
    case.clock[0] = datetime(2026, 10, 23, 7, 58, tzinfo=local_timezone).astimezone(UTC)
    merged_work(case, 20)
    observed = []
    for hour, minute in ((7, 59), (8, 0), (9, 59), (10, 0), (10, 0), (10, 30), (23, 59)):
        case.clock[0] = datetime(2026, 10, 23, hour, minute,
                                 tzinfo=local_timezone).astimezone(UTC)
        cycle(ctx)
        observed.append(len(pushes(outbox)))
    assert observed == [0, 0, 0, 1, 1, 1, 1], (
        f"notify_summary_hour=10 must replace the default hour and deliver once; got {observed}")
