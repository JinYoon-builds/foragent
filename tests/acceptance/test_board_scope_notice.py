"""Acceptance for r402 (gate side): a board write that fails for a missing token scope reaches the
person once, as one plain notice line with the fix, instead of only log lines.

The gate runs real ticks. Board calls go through GhTracker's GraphQL transport into a fake gh
that answers like GitHub does for a token without the `project` scope. Notices are read from the
journal the planning session reads (orch_inbox.recent_notices), so the route stays the gate's
existing one: whichever state folder the gate writes its notices to, the test reads both.
"""

from dataclasses import replace
from datetime import timedelta
import json
import subprocess

import pytest

from test_board_projects import initialized_case
from test_skills_autosync import isolated_home  # noqa: F401

FIX = "gh auth refresh -h github.com -s project"

SCOPE_STDOUT = json.dumps({"data": None, "errors": [{
    "type": "INSUFFICIENT_SCOPES",
    "locations": [{"line": 1, "column": 1}],
    "message": ("Your token has not been granted the required scopes to execute this query. The "
                "'updateProjectV2ItemFieldValue' field requires one of the following scopes: ['project'], "
                "but your token has only been granted the: ['gist', 'read:org', 'repo', 'workflow'] scopes. "
                "Please modify your token's scopes at: https://github.com/settings/tokens."),
}]})
SCOPE_STDERR = ("gh: Your token has not been granted the required scopes to execute this query. The "
                "'updateProjectV2ItemFieldValue' field requires one of the following scopes: ['project'], "
                "but your token has only been granted the: ['gist', 'read:org', 'repo', 'workflow'] scopes. "
                "Please modify your token's scopes at: https://github.com/settings/tokens.\n")
OTHER_STDERR = "gh: Something went wrong while executing your query. (HTTP 502)\n"


@pytest.fixture(autouse=True)
def offline_github(monkeypatch, isolated_home):
    from test_local_view import record_gh

    calls = record_gh(monkeypatch)
    yield
    assert calls == [], "Board tests must use the injected offline gh runner"


def failing_board(case, monkeypatch, stderr, stdout=""):
    """Every board GraphQL call after init fails the way gh fails for this token."""
    from foragent.tracker import GhTracker

    attempts = []

    def run(argv, input=None):
        assert argv == ["api", "graphql", "--input", "-"]
        attempts.append(json.loads(input)["query"])
        raise subprocess.CalledProcessError(1, ["gh", *argv], output=stdout, stderr=stderr)

    monkeypatch.setattr(case.tracker, "graphql", GhTracker("owner/repo", run=run).graphql)
    return attempts


def notices(case):
    from foragent.orch_inbox import recent_notices
    from foragent.state import StateStore

    since = case.clock[0] - timedelta(days=1)
    found = []
    seen = set()
    for store in (case.store, StateStore.for_repo("owner/repo")):
        if store.root in seen:
            continue
        seen.add(store.root)
        events, _problems = recent_notices(store, since)
        found += events
    return found


def ticks_with_board_writes(case, attempts):
    """Three ticks that each move the card; returns how many board calls each tick tried."""
    from foragent.scheduler import tick
    from test_orch_review_handoff import tick_context

    context = tick_context(case, start_runs=False)
    tried = []
    for step in (context,  # Ready: the card moves to Up next.
                 # Running: the card moves again. A fake launcher returning no PID starts nothing real.
                 replace(context, start_runs=True, launcher=lambda number: None),
                 context):
        before = len(attempts)
        assert tick(step).skipped is None
        tried.append(len(attempts) - before)
        case.clock[0] += timedelta(minutes=5, seconds=1)
    return tried


def ready_case(git_history, tmp_path, monkeypatch):
    case, _main, _argv, config = initialized_case(git_history, tmp_path, monkeypatch)
    case.ctx.config = replace(case.config, board=config["board"])
    case.store.finish_run(1, outcome="blocked")
    case.tracker.remove_labels(1, ["running", "approved"])
    case.tracker.record_label(1, "approved", "human")
    return case


def test_missing_scope_board_write_gives_the_person_one_plain_line_with_the_fix(
    git_history, tmp_path, monkeypatch,
):
    case = ready_case(git_history, tmp_path, monkeypatch)
    attempts = failing_board(case, monkeypatch, SCOPE_STDERR, SCOPE_STDOUT)

    tried = ticks_with_board_writes(case, attempts)

    assert sum(1 for count in tried if count) >= 2, f"Every card move tries the board again: {tried}"
    found = [event for event in notices(case) if FIX in event["summary"]]
    assert len(found) == 1, f"One notice for the missing scope, not one per failed write: {notices(case)}"
    summary = found[0]["summary"]
    assert "\n" not in summary.strip(), f"The person sees one plain line: {summary!r}"
    assert "Traceback" not in summary and "INSUFFICIENT_SCOPES" not in summary, summary
    assert case.tracker.get_issue(1).labels >= {"approved"}, "A board failure does not stop the work"


def test_other_board_failures_do_not_ask_for_the_project_scope(git_history, tmp_path, monkeypatch):
    case = ready_case(git_history, tmp_path, monkeypatch)
    attempts = failing_board(case, monkeypatch, OTHER_STDERR)

    tried = ticks_with_board_writes(case, attempts)

    assert sum(1 for count in tried if count) >= 2, f"Every card move tries the board again: {tried}"
    assert [event for event in notices(case) if FIX in event["summary"]] == [], (
        "Only a missing-scope failure asks the person to refresh the token")
