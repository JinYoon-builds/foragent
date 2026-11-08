"""Acceptance test for n73-n76-inspect-current-turn.

`fa inspect` listed every open Foragent PR whose issue lacked the blocked and
needs-human labels as "PR awaits a human merge decision" (inspect_data.collect).
That fallback ignored two recorded states:

- the issue is still running (its runner holds the run and the `running` label);
- the issue's latest park intent is parked. `fa orch park` removes the blocking
  labels while it records the intent, so a parked PR fell into the same list.
  This includes a park intent still pending in the local GitHub outbox.

Now running and currently parked work is not a person's merge decision in text
or JSON, while genuine review stays: an ordinary open PR, a PR whose older park
was explicitly unparked, and (regression control) the merge-queued exclusion.

Regression controls that already pass on main (an unparked PR and a finished
run stay review work) live in test_inspect_current_turn_guard.py.
"""

from datetime import UTC, datetime, timedelta

import pytest

from foragent import merge_owner
from inspect_current_turn_world import (  # noqa: F401  (world is a fixture)
    assert_counts_match, assert_not_merge_decision, assert_review, world,
)


def test_running_issue_pr_is_not_review_work(world, capsys):
    case = world()
    running_pr = case.issue_with_pr(1)
    case.running(1)
    review_pr = case.issue_with_pr(2)

    payload, lines = case.inspect(capsys)
    assert_not_merge_decision(payload, lines, 1, running_pr, "The issue is still running")
    assert payload["summary"]["counts"]["review"] == 1, payload["summary"]
    assert_counts_match(payload)
    assert_review(payload, lines, 2, review_pr)
    assert lines[0].startswith("Review 1 "), f"The text count line counts only #2 as review: {lines[0]}"


@pytest.mark.parametrize("for_person", [False, True])
def test_parked_issue_pr_is_not_a_merge_decision(world, capsys, for_person):
    case = world()
    parked_pr = case.issue_with_pr(1)
    review_pr = case.issue_with_pr(2)
    case.fa(capsys, "orch", "park", "1", "--why", "Wait for the scope decision",
            *(["--for-person"] if for_person else []))
    labels = case.tracker.get_issue(1).labels
    assert not labels & {"blocked", "needs-human"}, f"Setup: park removed the blocking labels: {labels}"
    assert not case.store.github_outbox(), "Setup: the park marker was delivered"

    payload, lines = case.inspect(capsys)
    assert_not_merge_decision(payload, lines, 1, parked_pr, "The issue's latest intent is parked")
    assert payload["summary"]["counts"]["review"] == 1, payload["summary"]
    assert_counts_match(payload)
    assert_review(payload, lines, 2, review_pr)


def test_pending_local_park_intent_is_not_a_merge_decision(world, capsys):
    case = world()
    parked_pr = case.issue_with_pr(1)
    review_pr = case.issue_with_pr(2)
    # GitHub is paused: the park intent waits in the local outbox, not yet on the issue.
    case.store.pause_github(datetime.now(UTC) + timedelta(hours=1), "rate limit")
    case.fa(capsys, "orch", "park", "1", "--why", "Wait for the scope decision")
    assert [entry for entry in case.store.github_outbox() if entry["action"] == "comment"], (
        "Setup: the park marker is still pending locally")
    assert not [c for c in case.tracker.list_comments(1) if "kind=parked" in c.body], (
        "Setup: no delivered park marker on the issue")

    payload, lines = case.inspect(capsys)
    assert_not_merge_decision(payload, lines, 1, parked_pr, "The pending local intent is parked")
    assert_counts_match(payload)
    assert_review(payload, lines, 2, review_pr)


def test_merge_queued_exclusion_still_holds(world, capsys, monkeypatch):
    case = world(merge_owner=True)
    queued_pr = case.issue_with_pr(1)
    review_pr = case.issue_with_pr(2)
    running_pr = case.issue_with_pr(3)
    case.running(3)
    monkeypatch.setattr(merge_owner, "pending", lambda store, n: n == 1)

    payload, lines = case.inspect(capsys)
    assert_not_merge_decision(payload, lines, 1, queued_pr, "The merge owner holds it")
    assert_not_merge_decision(payload, lines, 3, running_pr, "The issue is still running")
    assert_review(payload, lines, 2, review_pr)
    assert payload["summary"]["counts"]["review"] == 1
    assert_counts_match(payload)
