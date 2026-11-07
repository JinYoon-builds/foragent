"""Acceptance test for #2454 (narrowed, PM q59): a promise whose event is a PR closing is refused.

The gate does not observe a pull request closing, so ``fa orch promise add`` refuses such an event
when it is recorded, with a failing exit, and its message names the supported events in the
command's own syntax (``closed:``, ``merged:``, ``approved:``, ``time:``; see the contract in
tests/acceptance/test_promises_shelf.py). Nothing is recorded, and a later close of the PR raises
no promise notice.

Every check goes through the real ``fa`` CLI with an offline FakeTracker and real ``fa gate tick``
runs (tick_case). No process is started and no signal is sent.
"""

from __future__ import annotations

import pytest

from test_promises_shelf import listed, open_issue, promise, run, tick
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401 (fixture)
from test_tick_failure_alert import tick_case  # noqa: F401 (fixture)
from shelf_object_support import requests


SUPPORTED = ("closed:", "merged:", "approved:", "time:")


def open_pr(case):
    """Open a pull request the way GitHub lists it (an issue-number PR) and return its number."""
    return case.tracker.create_pr(head="feature/storage", base="main", title="Change storage",
                                  body="A pull request.")


@pytest.mark.parametrize("form", ["closed:{pr}", "pr-closed:{pr}"])
def test_a_promise_whose_event_is_a_pr_closing_is_refused_naming_the_supported_events(tick_case, capsys, form):
    case = tick_case
    open_issue(case, 2602)
    pr = open_pr(case)
    assert case.tracker.get_issue(pr).is_pull_request
    when = form.format(pr=pr)
    tick(case, capsys)
    kept = promise(case, capsys, "closed:2602", "publish order kite after #2602", "orchestrator")

    code, out, err = run(case, capsys, "orch", "promise", "add", "--when", when,
                         "--do", "publish order reed after the PR closes", "--owner", "orchestrator")
    message = out + err
    assert code != 0, f"A PR closing ({when!r}) is an event the gate does not observe; it is refused: {message}"
    missing = [kind for kind in SUPPORTED if kind not in message]
    assert not missing, f"The refusal names the supported events (missing {missing}): {message!r}"
    assert set(listed(case, capsys)) == {kept}, "A refused promise is not recorded"

    case.tracker.close_pr(pr)
    for _ in range(2):
        tick(case, capsys)
    assert set(listed(case, capsys)) == {kept}, "Nothing refused shows up later"
    assert not [row for row in requests(case, capsys)
                if str(row.get("kind", "")).startswith("promise-")], "A refused promise never notifies anyone"
