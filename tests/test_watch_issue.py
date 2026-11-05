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


def _result(store, result, number=7):
    store.clock[0] += timedelta(minutes=1)
    store.record_ci_result(number, result)


def _ticks(store, count):
    """Ordinary gate tick records written after the run: the history a replay would have to scan."""
    for _ in range(count):
        store.record_flow("tick", outcome="idle")


def test_ci_state_follows_the_current_run(tmp_path):
    store = _store(tmp_path)
    assert watch_issue.ci_state(store, 7) == "none"
    _step(store, "ci")
    assert watch_issue.ci_state(store, 7) == "running"
    _result(store, "passed")
    _step(store, "merging")
    assert watch_issue.ci_state(store, 7) == "passed", "the recorded result, not the step"
    _step(store, "implementing")
    _result(store, "failed")
    assert watch_issue.ci_state(store, 7) == "failed", "the run's latest conclusion"
    _step(store, "ci")
    assert watch_issue.ci_state(store, 7) == "running", "a live run back in CI"
    _step(store, "merge-lock-wait")
    _result(store, "passed")
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
RESULTS = [None, "passed", "no_checks", "failed", "timed_out", "not_started"]


def _expected(outcome, result):
    """The finished-run table: the run's recorded CI result as is; the outcome only fills its absence."""
    if result is not None:
        return result
    return "failed" if outcome == "ci-failed" else "unrecorded"


def _finish(store, outcome, step, result):
    if result is not None:
        _result(store, result)
    if step is not None:
        _step(store, step)
    store.clock[0] += timedelta(minutes=1)
    store.finish_run(7, outcome)
    assert store.get_run(7).phase == step, "the premise: the last step survives finish_run"


@pytest.mark.parametrize("ticks", [0, 2500], ids=["no-later-history", "2500-later-ticks"])
@pytest.mark.parametrize("result", RESULTS)
@pytest.mark.parametrize("outcome", OUTCOMES)
def test_every_terminal_outcome_and_ci_result_whatever_history_follows(tmp_path, outcome, result, ticks):
    """Review 6: a recorded pass (or failure) must survive thousands of later tick records."""
    store = _store(tmp_path)
    _finish(store, outcome, "ci", result)
    _ticks(store, ticks)
    assert watch_issue.ci_state(store, 7) == _expected(outcome, result)


@pytest.mark.parametrize("step", STEPS)
@pytest.mark.parametrize("outcome", OUTCOMES)
def test_every_terminal_outcome_in_every_stop_step(tmp_path, outcome, step):
    from foragent.config import Config
    from foragent.i18n import text
    from foragent.watch_sections import lane_bubble

    store = _store(tmp_path)
    _finish(store, outcome, step, "passed" if outcome in {"merged", "merge-queued"} else None)
    expected = "passed" if outcome in {"merged", "merge-queued"} else _expected(outcome, None)
    assert watch_issue.ci_state(store, 7) == expected

    def working(*args, **kwargs):
        raise AssertionError("a finished run is never described by the live acorn explanation")

    original = lane_bubble.explain
    lane_bubble.explain = working
    try:
        for language in ("en", "ko"):
            shown = watch_issue.plain(watch_issue.rows(None, store, Config(language=language), tmp_path, 7,
                                                       now=store.clock[0], width=120))
            assert text("issue_view_ci_running", language) not in shown
            assert text(f"issue_view_ci_{expected}", language) in shown
            assert shown.count(text("issue_view_ended", language, outcome="")) == 1, shown
    finally:
        lane_bubble.explain = original


def test_the_result_lives_in_history_after_the_next_run_starts(tmp_path):
    store = _store(tmp_path)
    _step(store, "ci")
    _result(store, "passed")
    store.clock[0] += timedelta(minutes=1)
    finished = store.finish_run(7, "crashed")
    store.clock[0] += timedelta(minutes=1)
    store.start_run(7, branch="foragent/issue-7", worktree=tmp_path / "wt")
    assert store.ci_result(7, finished.started_at) == "passed", "kept as long as the run record"
    _step(store, "resolving-conflict")
    assert watch_issue.ci_state(store, 7) == "none", "a new run starts without the earlier run's result"
    store.clock[0] += timedelta(minutes=1)
    store.finish_run(7, "timeout")
    assert watch_issue.ci_state(store, 7) == "unrecorded"


def test_a_runs_own_ci_failure_record_counts_when_it_has_no_result(tmp_path):
    """Runs that predate ci_result kept only their per-run ci-failure record; a ci_result always wins."""
    store = _store(tmp_path)
    _step(store, "implementing")
    _fail_ci(store)
    assert watch_issue.ci_state(store, 7) == "failed"
    store.record_flow("ci-failure", issue=8, run_started_at=store.get_run(7).started_at, pr=1)
    _result(store, "passed")
    assert watch_issue.ci_state(store, 7) == "passed", "the run's latest recorded result wins"


def test_only_the_live_run_writes_its_result(tmp_path):
    store = _store(tmp_path)
    _step(store, "ci")
    _result(store, "passed")
    store.clock[0] += timedelta(minutes=1)
    store.finish_run(7, "merge-queued")
    record = (store.root / "runs" / "7.json").read_bytes()
    store.record_ci_result(7, "failed")  # e.g. a later merge-owner read: not this run's writer
    assert (store.root / "runs" / "7.json").read_bytes() == record, "a finished record is never rewritten"
    assert watch_issue.ci_state(store, 7) == "passed"
    with pytest.raises(ValueError):
        store.record_ci_result(7, "running")


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
