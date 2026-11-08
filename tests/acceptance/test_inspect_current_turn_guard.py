"""Regression controls for n73-n76-inspect-current-turn (pass on main; not in Done when).

Genuine human review stays visible: a PR whose older park intent was explicitly
unparked, and a PR whose run has finished.
"""

from inspect_current_turn_world import assert_counts_match, assert_review, world  # noqa: F401


def test_unparked_issue_pr_still_awaits_human_review(world, capsys):
    case = world()
    pr = case.issue_with_pr(1)
    case.fa(capsys, "orch", "park", "1", "--why", "Wait for the scope decision")
    case.fa(capsys, "orch", "unpark", "1")
    case.tracker.add_labels(1, ["approved"])

    payload, lines = case.inspect(capsys)
    assert_review(payload, lines, 1, pr)
    assert payload["summary"]["counts"]["review"] == 1, payload["summary"]
    assert_counts_match(payload)


def test_finished_run_pr_awaits_human_review(world, capsys):
    """A run that finished leaves its open PR to the person (regression control)."""
    case = world()
    pr = case.issue_with_pr(1)
    case.running(1)
    case.store.finish_run(1, outcome="pr-open")
    case.tracker.remove_labels(1, ["running"])

    payload, lines = case.inspect(capsys)
    assert_review(payload, lines, 1, pr)
    assert payload["summary"]["counts"]["review"] == 1
