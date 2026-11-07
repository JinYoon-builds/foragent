"""Runner cases for the #1514 timeout names, shared by unit tests (the locked file keeps its own copy)."""

from foragent.reasons import marked_reason
from foragent.runner import run_issue

from helpers.review_case import make_case


RUNTIME_MINUTES = 90


def blocker_reasons(case, n=1):
    """Reason codes of the blocker markers on the issue, oldest first."""
    return [code for comment in case.tracker.list_comments(n)
            if (code := marked_reason(comment.body)) is not None]


def run_and_observe(case):
    outcome = run_issue(1, case.ctx)
    run = case.store.get_run(1)
    issue = case.tracker.get_issue(1)
    return {
        "outcome": outcome,
        "stored_outcome": run.outcome,
        "detail": run.detail,
        "reasons": blocker_reasons(case),
        "flag": case.store.load_flag(1),
        "state": issue.state,
        "labels": sorted(issue.labels & {"blocked", "needs-human", "ready", "running"}),
    }


def runner_case(git_history, tmp_path, monkeypatch):
    return make_case(git_history, tmp_path, monkeypatch, skill_libraries=[],
                     max_runtime_minutes=RUNTIME_MINUTES, merge_owner=True)
