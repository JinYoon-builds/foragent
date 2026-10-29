"""Decide whether `fa init` offers a starter order (#848).

The offer is only for a project with no Foragent work yet: no open approved
issue and no merged PR from a Foragent branch. Unapproved issues and other
PRs are not Foragent work.
"""

from foragent.tracker import pr_issue_number


def eligible(tracker) -> bool:
    """Return True when the tracker shows no Foragent work. Lookup errors propagate."""
    if any("approved" in issue.labels for issue in tracker.list_issues("open")
           if not issue.is_pull_request):
        return False
    return not any(pr_issue_number(pr.head) is not None for pr in tracker.list_prs(state="merged"))
