"""Acceptance test for own-refresh-head. Locked by Foragent; do not edit.

With the merge owner on, a queued PR whose base moved is refreshed: the base is
merged into the verified head, the new head is verified and pushed with a lease
on the head it replaces. GitHub reports a PR's new head only some seconds after
a push; until then a PR read still shows the head the push replaced. That read
is Foragent's own push not yet visible, not a foreign push, so it must not hold
the PR as "PR head changed after verification". A head that neither the run nor
the merge owner produced still holds, as before.

Real runner, gate tick, merge owner, Git and state; only GitHub, models and the
clock are fake (the merge-owner prototype world).
"""

from acceptance.test_merge_path_real_settings import world  # noqa: F401
from conftest import git_command
from test_merge_owner_proto import proto  # noqa: F401
import test_merge_owner_proto as support
from foragent import merge_owner
from foragent.reasons import Reason, parse_reason

CHANGED = "PR head changed after verification"
LAG_READS = 2


def lagging_reads(case, monkeypatch, pr):
    """GitHub shows the replaced head for the next LAG_READS reads after Foragent's next push.

    Only the first head change after this call lags; any later head change (for
    example another actor's push) is visible at once and ends the lag.
    """
    tracker = case.tracker
    set_head, head_of = tracker.set_pr_head_sha, tracker.pr_head_sha
    lag = {"armed": True, "replaced": None, "left": 0, "served": 0}

    def pushed(number, head):
        previous = head_of(number)
        set_head(number, head)
        if number != pr or previous == head:
            return
        if lag["armed"]:
            lag.update(armed=False, replaced=previous, left=LAG_READS)
        else:
            lag["left"] = 0

    def reported(number):
        if number == pr and lag["left"] > 0:
            lag["left"] -= 1
            lag["served"] += 1
            return lag["replaced"]
        return head_of(number)

    monkeypatch.setattr(tracker, "set_pr_head_sha", pushed)
    monkeypatch.setattr(tracker, "pr_head_sha", reported)
    return lag


def remote_head(case, n):
    return git_command(case.history.origin, "rev-parse", f"refs/heads/foragent/issue-{n}")


def held_as_changed_head(case, n):
    return any(CHANGED in comment.body for comment in case.tracker.list_comments(n))


def test_own_refresh_with_lagging_pr_head_read_still_merges(proto, monkeypatch):
    case = proto
    support.queue_workers(case, [5])
    pr = support.pr_for(case, 5).number
    queued = remote_head(case, 5)
    lag = lagging_reads(case, monkeypatch, pr)
    case.history.update({"src/neighbor.py": "VALUE = 'moved during the queue'\n"})

    support.drain(case, count=1)

    refreshed = remote_head(case, 5)
    assert refreshed != queued, "The moved base must be merged into the PR by a refresh push"
    assert lag["served"] >= 1, "The test must expose the replaced head after the refresh push"
    assert not held_as_changed_head(case, 5), (
        "A PR read that still shows the head Foragent's own leased push replaced is not a changed head")
    assert "blocked" not in case.tracker.get_issue(5).labels
    assert case.tracker.merged_prs == [pr], "The refreshed PR merges once after its CI passes"
    assert case.merges[-1]["head"] == refreshed, "The merge is of the head the refresh verified and pushed"
    assert case.tracker.get_issue(5).state == "closed"
    assert git_command(case.history.origin, "show", "main:src/neighbor.py") == "VALUE = 'moved during the queue'"


def test_foreign_push_after_own_refresh_still_holds(proto, monkeypatch):
    case = proto
    support.queue_workers(case, [5])
    pr = support.pr_for(case, 5).number
    queued = remote_head(case, 5)
    lagging_reads(case, monkeypatch, pr)
    case.history.update({"src/neighbor.py": "VALUE = 'moved during the queue'\n"})

    support.gate(case)  # The owner refreshes the PR onto the moved base and waits for its CI.

    refreshed = remote_head(case, 5)
    assert refreshed != queued, "The moved base must be merged into the PR by a refresh push"
    assert not held_as_changed_head(case, 5), "The owner's own refresh push is not a changed head"
    assert merge_owner.admissions(case.store), "The refreshed PR stays queued for its CI round"
    assert not case.tracker.merged_prs

    foreign = support.update_pr_branch(case, 5)  # Someone else pushes to the PR branch.
    support.drain(case, count=1)

    assert not case.tracker.merged_prs, "A head nobody in Foragent produced is never merged"
    assert remote_head(case, 5) == foreign, "The foreign commit is kept, not overwritten"
    issue = case.tracker.get_issue(5)
    assert issue.state == "open" and "blocked" in issue.labels
    comment = case.tracker.list_comments(5)[-1].body
    assert parse_reason(comment) == Reason.RISK_HIGH
    assert CHANGED in comment
    assert not merge_owner.admissions(case.store), "The held PR leaves the merge queue"
