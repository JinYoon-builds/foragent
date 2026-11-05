"""Acceptance tests for #1514: the result name ``timeout`` means one fact only.

Over 7 days, 37 runs ended as ``timeout``: 21 because the base kept moving,
14 because the work was too large for one session, 2 because a time limit
was really reached. Each fact gets its own name; ``timeout`` stays for a check
or CI that reached its own time limit. No time limit changes.

Names (decided 2026-11-04):
- ``too-large``: the run's own work used the whole run budget. It waits for a
  person and still says to split the work.
- ``base-changed``: the run's work fit, and the budget went to following a
  moving base. The issue returns to ready by itself: no block, no person.
- ``runtime-exceeded``: the gate's runtime backstop stopped a runner. The gate
  cannot tell a moving base from too-large work, so the name says neither.

Real runner, real Git history, real budget clock, merge owner on (as in this
repository's configuration); only external processes (executors, Done when /
test subprocesses) are faked.
"""

from datetime import timedelta

import pytest

from foragent.reasons import GROUP, Reason, marked_reason
from foragent.runner import run_issue
from test_machine_sleep import PID, sleep_case, tick_case
from test_orch_review_handoff import make_case


RUNTIME_MINUTES = 90
TOO_LARGE = "too-large"
BACKSTOP = "runtime-exceeded"


def blocker_reasons(case, n=1):
    """Reason codes of the blocker markers on the issue, oldest first."""
    return [code for comment in case.tracker.list_comments(n)
            if (code := marked_reason(comment.body)) is not None]


def blocker_text(case, n=1):
    return "\n".join(comment.body for comment in case.tracker.list_comments(n)
                     if marked_reason(comment.body) is not None)


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


def too_big_case(git_history, tmp_path, monkeypatch, session):
    """One agent session uses the whole run budget; the base never moves."""
    case = runner_case(git_history, tmp_path, monkeypatch)

    def whole_session(kwargs):
        case.clock[0] += timedelta(minutes=RUNTIME_MINUTES, seconds=1)

    if session == "implementer":
        def implement_all_day(kwargs):
            case.implement(kwargs)
            whole_session(kwargs)
            return "Implemented, but it took the whole session"
        case.implementer.steps = [implement_all_day]
    else:
        case.implementer.steps = [case.implement] * (case.config.max_retries + 1)

        def review_all_day(kwargs):
            whole_session(kwargs)
            return "VERDICT: PASS"
        case.reviewer.steps = [review_all_day] * (case.config.review_max + 2)
    return case


def moving_base_case(git_history, tmp_path, monkeypatch):
    """The work fits and passes review; then main moves and following it uses up the run.

    The reviewer's PASS is the moment main advances (another lane merged).
    Re-verifying on the new base is what spends the rest of the budget.
    """
    case = runner_case(git_history, tmp_path, monkeypatch)
    case.implementer.steps = [case.implement] * (case.config.max_retries + 1)
    moves = []

    def review_then_base_moves(kwargs):
        # Another lane's merge touches source this run's checks depend on.
        git_history.update({"src/other_lane.py": f"VALUE = {len(moves) + 1}\n"},
                           message="Another lane merged")
        moves.append(git_history.tip)
        # Most of the budget is still left when the review passes.
        case.clock[0] += timedelta(minutes=10)
        return "VERDICT: PASS"

    case.reviewer.steps = [review_then_base_moves] * (case.config.review_max + 2)

    from foragent import runner
    real_run_logged = runner.run_logged

    def check_process(argv, *, timeout_s, **kwargs):
        if moves:
            # Checks for the moved base run until the run budget is gone.
            case.clock[0] += timedelta(minutes=RUNTIME_MINUTES)
        return real_run_logged(argv, timeout_s=timeout_s, **kwargs)

    monkeypatch.setattr(runner, "run_logged", check_process)
    case.moves = moves
    return case


@pytest.mark.parametrize("session", ["implementer", "reviewer"])
def test_too_big_for_one_session_has_its_own_name_not_timeout(git_history, tmp_path, monkeypatch, session):
    case = too_big_case(git_history, tmp_path, monkeypatch, session)

    observed = run_and_observe(case)

    assert observed["outcome"] == observed["stored_outcome"] == TOO_LARGE, (
        "Work too large for one session reached no time limit of a check: its result is "
        f"{TOO_LARGE!r}, not 'timeout' and not 'base-changed'. Observed: {observed}"
    )
    assert observed["reasons"] == [TOO_LARGE], f"Exactly one blocker marker, named {TOO_LARGE!r}: {observed}"
    assert Reason(TOO_LARGE) in GROUP, "The too-large reason must be a grouped reason this version knows"
    assert observed["flag"] is not None and observed["flag"].reason == TOO_LARGE, observed
    assert observed["state"] == "open" and "blocked" in observed["labels"], (
        f"Work too large for one session waits for a person: {observed}"
    )
    assert "split" in blocker_text(case).casefold(), (
        f"The person is still told to split the work: {blocker_text(case)!r}"
    )


def test_base_kept_moving_is_a_base_change_not_a_timeout_or_too_large(git_history, tmp_path, monkeypatch):
    case = moving_base_case(git_history, tmp_path, monkeypatch)

    observed = run_and_observe(case)

    assert case.moves, f"The scenario must move main during the run: {observed}"
    assert observed["outcome"] == observed["stored_outcome"] == "base-changed", (
        "The run's own work fit and passed review; its budget went to following a moving base. "
        f"That is 'base-changed', not 'timeout' and not {TOO_LARGE!r}. Observed: {observed}"
    )
    assert observed["reasons"] == [] and observed["flag"] is None, (
        f"A moving base needs no person: no blocker marker and no flag. Observed: {observed}"
    )
    assert observed["state"] == "open" and observed["labels"] == ["ready"], (
        f"The issue returns to ready by itself, not blocked, not left running: {observed}"
    )
    assert "split" not in (observed["detail"] or "").casefold(), (
        f"A moving base must not tell anyone to split work that fit: {observed}"
    )


def test_gate_runtime_backstop_has_a_neutral_name(git_history, tmp_path, monkeypatch):
    """The gate stops a runner past its runtime limit; it cannot see why, so it names neither cause."""
    case = sleep_case(git_history, tmp_path, monkeypatch, stall_minutes=2000)
    case.facts.advance((RUNTIME_MINUTES + 11) * 60)
    case.store.update_run(1, last_progress_at=case.clock[0].isoformat())

    tick_case(case)

    run = case.store.get_run(1)
    assert run.status == "finished" and run.outcome == "killed" and case.killed == [PID], (
        f"The backstop itself is unchanged: it still stops the runner. outcome={run.outcome}"
    )
    assert blocker_reasons(case) == [BACKSTOP], blocker_reasons(case)
    assert case.store.load_flag(1).reason == BACKSTOP
    assert Reason(BACKSTOP) in GROUP
    text = blocker_text(case).casefold()
    assert "split" not in text and "too big" not in text and "base" not in text, (
        f"The backstop cannot tell a moving base from too-large work; it must claim neither: {text!r}"
    )
