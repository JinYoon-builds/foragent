from datetime import UTC, datetime, timedelta
import subprocess

import pytest

from foragent import watch_issue
from foragent.state import StateStore

NOW = datetime(2026, 11, 5, 12, tzinfo=UTC)


def _store(tmp_path):
    clock = [NOW]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    store.start_run(7, branch="foragent/issue-7", worktree=tmp_path / "wt")
    store.clock = clock
    return store


def _step(store, phase, minutes=1):
    """Move the run to ``phase`` a few minutes later, as the runner does (the old phase is journaled)."""
    store.clock[0] += timedelta(minutes=minutes)
    store.update_run(7, phase=phase, phase_started_at=store.clock[0].isoformat())


def _fail_ci(store):
    store.clock[0] += timedelta(minutes=1)
    store.record_flow("ci-failure", issue=7, run_started_at=store.get_run(7).started_at, pr=1)


def _read(store, status, number=7):
    store.clock[0] += timedelta(minutes=1)
    store.record_flow("ci-read", issue=number, run_started_at=store.get_run(number).started_at, pr=1, status=status)


def test_ci_state_follows_the_current_run(tmp_path):
    store = _store(tmp_path)
    assert watch_issue.ci_state(store, 7) == "none"
    _step(store, "ci")
    assert watch_issue.ci_state(store, 7) == "running"
    _read(store, "pass")
    _step(store, "merging")
    assert watch_issue.ci_state(store, 7) == "passed", "the recorded read, not the step"
    _step(store, "implementing")
    store.record_flow("ci-failure", issue=7, run_started_at="2000-01-01T00:00:00+00:00", pr=1)
    assert watch_issue.ci_state(store, 7) == "passed", "another run's failure is not this run's"
    _fail_ci(store)
    assert watch_issue.ci_state(store, 7) == "failed", "the newest CI fact wins"
    _step(store, "merge-lock-wait")
    _read(store, "pending")
    assert watch_issue.ci_state(store, 7) == "running", "a live run whose last read was pending"
    _read(store, "pass")
    assert watch_issue.ci_state(store, 7) == "passed", "a later attempt passed after the failure"
    assert watch_issue.ci_state(store, 8) == "none"


# Every way a run ends (`finish_run` callers, `ExecutorStopped` reasons, publish returns).
OUTCOMES = ["merged", "merge-queued", "pr-open", "held", "ci-failed", "retries-exceeded", "review-failed",
            "spec-question", "timeout", "limit-wait", "stopped", "killed", "crashed", "stalled-restart",
            "hash-mismatch", "base-changed", "executor-error", "error", "merge-wait", "remote-ended",
            "pr-body-values-missing", "issue-closed", "superseded"]
# Every step a run can be in when it ends.
STEPS = [None, "baseline", "implementing", "resolving", "checking", "reviewing", "ci", "merge-turn-wait",
         "merge-lock-wait", "merging", "resolving-conflict", "overlap-wait", "test-slot-wait",
         "baseline-wait", "limit-wait", "executor-wait", "run-slot-wait"]
# What this run recorded about CI before it ended, oldest first: a read's status, or a code failure.
HISTORIES = {"nothing": [], "pass": ["pass"], "fail": ["fail"], "no-checks": ["none"], "pending": ["pending"],
             "not-started": ["fail", "not-started"], "failure": ["ci-failure"], "pass-then-failure": ["pass", "ci-failure"],
             "failure-then-pass": ["ci-failure", "pending", "pass"], "pass-then-final-fail": ["pass", "fail"]}
WORDS = {"pass": "passed", "fail": "failed", "none": "no_checks", "not-started": "not_started", "ci-failure": "failed"}


def _history(store, entries):
    for entry in entries:
        if entry == "ci-failure":
            _fail_ci(store)
        else:
            _read(store, entry)


def _expected(outcome, history):
    """The finished-run table: the newest recorded CI fact, never the outcome's or the step's meaning."""
    newest = (HISTORIES[history] or [None])[-1]
    if newest in WORDS:
        return WORDS[newest]
    return "failed" if outcome == "ci-failed" else "unrecorded"


@pytest.mark.parametrize("history", list(HISTORIES))
@pytest.mark.parametrize("step", STEPS)
@pytest.mark.parametrize("outcome", OUTCOMES)
def test_every_terminal_outcome_in_every_stop_step(tmp_path, outcome, step, history):
    from foragent.config import Config
    from foragent.i18n import text
    from foragent.watch_sections import lane_bubble

    store = _store(tmp_path)
    _history(store, HISTORIES[history])
    if step is not None:
        _step(store, step)
    store.clock[0] += timedelta(minutes=1)
    store.finish_run(7, outcome)
    assert store.get_run(7).phase == step, "the premise: the last step survives finish_run"
    assert watch_issue.ci_state(store, 7) == _expected(outcome, history)

    def working(*args, **kwargs):
        raise AssertionError("a finished run is never described by the live acorn explanation")

    original = lane_bubble.explain
    lane_bubble.explain = working
    try:
        for language in ("en", "ko"):
            shown = watch_issue.plain(watch_issue.rows(None, store, Config(language=language), tmp_path, 7,
                                                       now=store.clock[0], width=120))
            assert text("issue_view_ci_running", language) not in shown
            assert shown.count(text("issue_view_ended", language, outcome="")) == 1, shown
    finally:
        lane_bubble.explain = original


def test_a_journaled_move_past_ci_without_a_read_is_not_a_pass(tmp_path):
    """Review 5: checks=ci with billing-blocked jobs; the run left `ci` for the merge turn, then was held."""
    store = _store(tmp_path)
    _step(store, "ci")
    _step(store, "merge-lock-wait")  # journals `ci -> merge-lock-wait`
    store.clock[0] += timedelta(minutes=1)
    store.finish_run(7, "held")
    assert watch_issue.ci_state(store, 7) == "unrecorded"
    _read(store, "fail")
    _read(store, "not-started")
    assert watch_issue.ci_state(store, 7) == "not_started"


def test_the_final_read_before_a_stop_decides(tmp_path):
    """Review 5: CI passed, a rerun failed at the final merge read, then approval was removed."""
    store = _store(tmp_path)
    _step(store, "ci")
    _read(store, "pass")
    _step(store, "merge-lock-wait")
    _read(store, "fail")
    store.clock[0] += timedelta(minutes=1)
    store.finish_run(7, "hash-mismatch")
    assert watch_issue.ci_state(store, 7) == "failed"


def test_an_earlier_runs_reads_are_not_this_runs(tmp_path):
    store = _store(tmp_path)
    _step(store, "ci")
    _read(store, "pass")
    store.clock[0] += timedelta(minutes=1)
    store.finish_run(7, "crashed")
    store.clock[0] += timedelta(minutes=1)
    store.start_run(7, branch="foragent/issue-7", worktree=tmp_path / "wt")
    _step(store, "resolving-conflict")
    assert watch_issue.ci_state(store, 7) == "none"
    store.clock[0] += timedelta(minutes=1)
    store.finish_run(7, "timeout")
    assert watch_issue.ci_state(store, 7) == "unrecorded"


def test_a_failure_recorded_with_a_pass_read_wins_at_the_same_time(tmp_path):
    """A CI comparison can fail a head GitHub passed; both facts may carry the same clock."""
    store = _store(tmp_path)
    _step(store, "ci")
    started = store.get_run(7).started_at
    store.record_flow("ci-read", issue=7, run_started_at=started, pr=1, status="pass")
    store.record_flow("ci-failure", issue=7, run_started_at=started, pr=1, head="abc")
    store.finish_run(7, "retries-exceeded")
    assert watch_issue.ci_state(store, 7) == "failed"


def test_ci_lines_state_a_result_and_never_what_the_lane_is_doing():
    from foragent.messages.issue_view import MESSAGES

    for language, activity in (("en", ("again", "working", "retry")), ("ko", ("다시", "하고 있어요"))):
        for key, line in MESSAGES[language].items():
            if key.startswith("issue_view_ci_") and key != "issue_view_ci_running":
                assert not any(word in line for word in activity), (language, key, line)


def test_known_and_infer(tmp_path):
    store = _store(tmp_path)
    assert watch_issue.known(store, 7) and not watch_issue.known(store, 8)
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "foragent/issue-12", str(repo)], check=True)
    assert watch_issue.infer(repo) == 12
    other = tmp_path / "other"
    subprocess.run(["git", "init", "-q", "-b", "main", str(other)], check=True)
    assert watch_issue.infer(other) is None
    assert watch_issue.infer(tmp_path / "missing") is None
