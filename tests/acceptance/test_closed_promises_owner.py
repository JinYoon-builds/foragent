"""Acceptance for closed-promises-stay-green (D122) on the merge owner's reading.

PM answer: every comparison that reads acceptance reports (run, merge owner,
merge batch) observes main's results through one shared reader. Here the run
reads a green main; then main breaks the locked test of closed issue #78 and
only the merge owner's comparison of the refreshed PR reads that result.

The world is the merge-owner prototype world (real runner, gate tick, merge
owner, Git, state and pytest measurements; fake GitHub, models and clock).
Issue #78 is that world's closed, merged owner of tests/acceptance/test_contract.py.
"""

from acceptance.test_merge_path_real_settings import CONTRACT, world  # noqa: F401
from conftest import git_command
from test_merge_owner_proto import proto  # noqa: F401
import test_merge_owner_proto as support

from test_closed_promises_stay_green import cleared, promise_reports, requests, _text

SHIPPED = 78
PROMISE = f"{CONTRACT}::test_contract"


def remote_head(case, n):
    return git_command(case.history.origin, "rev-parse", f"refs/heads/foragent/issue-{n}")


def test_merge_owner_comparison_reports_a_closed_issues_test_failing_on_main(proto):
    case = proto
    assert case.tracker.get_issue(SHIPPED).state == "closed"
    support.queue_workers(case, [5])
    pr = support.pr_for(case, 5).number
    queued = remote_head(case, 5)
    assert promise_reports(case, PROMISE, SHIPPED) == [], "main still keeps the promise when the run reads it"
    case.tracker.set_pr_checks(pr, "pending")  # The PR waits while main moves.

    # Main changes source and breaks #78's locked test; the owner refreshes the PR.
    case.history.update({"src/value.py": "VALUE = 'broken on main'\n"}, message="Main breaks a shipped promise")
    support.gate(case)
    assert remote_head(case, 5) != queued, "a source move refreshes the PR onto the moved base"
    case.tracker.set_pr_checks(pr, "pass")
    support.drain(case, count=1)

    assert case.tracker.merged_prs == [pr], "the test fails on main too, so the PR loses nothing and merges"
    reports = promise_reports(case, PROMISE, SHIPPED)
    assert len(reports) == 1, (
        f"the merge owner's reading must report {PROMISE} of #{SHIPPED} once; requests: {_text(requests(case))}")
    assert cleared(case, PROMISE, SHIPPED) == []
