"""Draft lock: GitHub's bare ``[remote rejected] ... (failed)`` push answer waits like a 5xx.

#1985 made a GitHub server error on ``git push`` pause and resume. On 2026-11-04
GitHub also refused issue branch pushes with ``! [remote rejected] <ref> (failed)``
and no other reason (runs #1801 and #2052); a retry an hour later pushed the same
head. That answer names no rule, permission or lease problem, so it is the same
kind of server-side fault. Rule, permission and stale-lease refusals stay
immediate failures (locked in test_git_push_outage.py).
"""

import pytest

from test_git_push_outage import BRANCH, Remote, drive, held_by, history, start
from test_github_drop import assert_merged_once, outage_case
from test_skills_autosync import isolated_home  # noqa: F401


# The text GitHub returned for #2052 on 2026-11-04T21:53Z.
FAILED = (
    "To https://github.com/owner/repo\n"
    f" ! [remote rejected]   {BRANCH} -> {BRANCH} (failed)\n"
    "error: failed to push some refs to 'https://github.com/owner/repo'\n"
)


@pytest.mark.parametrize("entry", ["run", "review"])
def test_bare_failed_ref_push_waits_then_publishes_the_same_head(git_history, tmp_path, monkeypatch, entry):
    case = outage_case(git_history, tmp_path, monkeypatch, entry)
    initial = start(case, entry)
    remote = Remote(case, [FAILED])
    began = case.clock[0]

    outcomes = drive(case, initial, "(failed)")

    assert "error" not in outcomes, f"GitHub's bare (failed) push answer is not an issue failure: {outcomes}"
    assert outcomes[-1] == "merged", outcomes
    assert_merged_once(case)
    assert not held_by(case), "No person or orchestrator retry was needed"
    assert not any(row.get("outcome") == "error" for row in history(case))
    assert case.clock[0] > began, "The push was repeated after a wait, not in a hot loop"
    assert remote.landed and remote.landed[-1] == remote.tried[0], "The same verified head is pushed again"
