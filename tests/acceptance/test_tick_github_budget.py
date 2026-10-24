"""Acceptance test for tick-github-budget. Locked by Foragent; do not edit."""

from collections import Counter
from dataclasses import replace
from datetime import timedelta

import pytest

from test_board_projects import initialized_case
from test_board_status_groups import record_board_gh
from test_skills_autosync import isolated_home  # noqa: F401  (fixture)

PER_ISSUE = ("get_issue", "label_events", "blocked_by", "find_pr", "list_comments",
             "parent_of", "sub_issues")
DEPENDENTS = range(30, 42)
BLOCKER = 50
DEPENDENT_BODY = ("<!-- foragent:v1 -->\n## Goal\nWait for the blocker.\n## Out of scope\nNothing.\n"
                  "## Done when\n```sh\ntrue\n```\n")


@pytest.fixture(autouse=True)
def offline_github(monkeypatch, isolated_home):  # noqa: F811
    from test_local_view import record_gh

    calls = record_gh(monkeypatch)
    yield
    assert calls == [], "Use the injected offline gh runner"


def budget_case(git_history, tmp_path, monkeypatch):
    """A board-enabled repo with twelve approved issues waiting on one open blocker."""
    from foragent.scheduler import tick
    from test_orch_review_handoff import tick_context

    case, main, argv, config = initialized_case(git_history, tmp_path, monkeypatch)
    case.ctx.config = replace(case.config, board=config["board"])
    case.store.finish_run(1, outcome="blocked")
    case.tracker.remove_labels(1, ["running", "approved"])
    case.tracker.add_issue(BLOCKER, "Blocker still in progress")
    for number in DEPENDENTS:
        # A real approved issue has a machine check; an empty body is blocked as no-machine-check.
        case.tracker.add_issue(number, f"Dependent {number}", DEPENDENT_BODY)
        case.tracker.record_label(number, "approved", "human")
        case.tracker.add_blocked_by(number, BLOCKER)
    calls = record_board_gh(case.tracker, monkeypatch)
    reads = Counter()
    for name in PER_ISSUE:
        original = getattr(case.tracker, name, None)
        if original is None:
            continue

        def counted(*args, _name=name, _original=original, **kwargs):
            reads[_name, args[0] if args else None] += 1
            return _original(*args, **kwargs)

        monkeypatch.setattr(case.tracker, name, counted)
    context = tick_context(case, start_runs=False)
    for _ in range(2):  # Establish snapshots, approvals and board cards.
        assert tick(context).skipped is None
        case.clock[0] += timedelta(minutes=5, seconds=1)
    return case, context, calls, reads


def test_tick_github_budget_unchanged_refresh_makes_no_per_issue_or_board_calls(
    git_history, tmp_path, monkeypatch,
):
    from foragent.scheduler import tick

    case, context, calls, reads = budget_case(git_history, tmp_path, monkeypatch)
    calls.clear()
    reads.clear()
    assert tick(context).skipped is None
    assert calls == [], f"Nothing changed, yet the tick used the board {len(calls)} times"
    assert sum(reads.values()) == 0, f"Nothing changed, yet the tick read issues one by one: {dict(reads)}"
    for number in DEPENDENTS:
        assert "ready" not in case.tracker.get_issue(number).labels


def test_tick_github_budget_one_changed_issue_reads_only_that_issue(
    git_history, tmp_path, monkeypatch,
):
    from foragent.scheduler import tick

    case, context, calls, reads = budget_case(git_history, tmp_path, monkeypatch)
    case.tracker.record_label(DEPENDENTS[0], "needs-human", "human")
    calls.clear()
    reads.clear()
    assert tick(context).skipped is None
    touched = {number for (_, number) in reads}
    assert touched <= {DEPENDENTS[0]}, f"Unchanged issues were read again: {sorted(touched - {DEPENDENTS[0]})}"
    assert len(calls) <= 6, f"One card change used {len(calls)} board calls"


def test_tick_github_budget_closed_blocker_still_releases_its_dependents(
    git_history, tmp_path, monkeypatch,
):
    from foragent.scheduler import tick

    case, context, calls, reads = budget_case(git_history, tmp_path, monkeypatch)
    case.tracker.close_issue(BLOCKER)
    case.clock[0] += timedelta(minutes=5, seconds=1)
    assert tick(context).skipped is None
    released = [number for number in DEPENDENTS if "ready" in case.tracker.get_issue(number).labels]
    assert released == list(DEPENDENTS), "Skipping unchanged issues must not miss a closed blocker"
