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
    """Move the run to ``phase`` a few minutes later, as the runner does."""
    store.clock[0] += timedelta(minutes=minutes)
    store.update_run(7, phase=phase, phase_started_at=store.clock[0].isoformat())


def _shown(store, tmp_path, language, explain):
    from foragent.config import Config
    from foragent.watch_sections import lane_bubble

    original = lane_bubble.explain
    lane_bubble.explain = explain
    try:
        return watch_issue.plain(watch_issue.rows(None, store, Config(language=language), tmp_path, 7,
                                                  now=store.clock[0], width=120))
    finally:
        lane_bubble.explain = original


# Every way a run ends (`finish_run` callers, `ExecutorStopped` reasons, publish returns).
OUTCOMES = ["merged", "merge-queued", "pr-open", "held", "ci-failed", "retries-exceeded", "review-failed",
            "spec-question", "timeout", "limit-wait", "stopped", "killed", "crashed", "stalled-restart",
            "hash-mismatch", "base-changed", "executor-error", "error", "merge-wait", "remote-ended",
            "pr-body-values-missing", "issue-closed", "superseded"]
# Every step a run can be in when it ends.
STEPS = [None, "baseline", "implementing", "resolving", "checking", "reviewing", "ci", "merge-turn-wait",
         "merge-lock-wait", "merging", "resolving-conflict", "overlap-wait", "test-slot-wait",
         "baseline-wait", "limit-wait", "executor-wait", "run-slot-wait"]


@pytest.mark.parametrize("step", STEPS)
@pytest.mark.parametrize("outcome", OUTCOMES)
def test_every_terminal_outcome_in_every_stop_step(tmp_path, outcome, step):
    """A finished run says what it ended with, never what its last step was doing."""
    from foragent.i18n import MESSAGES, text

    store = _store(tmp_path)
    if step is not None:
        _step(store, step)
    store.clock[0] += timedelta(minutes=1)
    store.finish_run(7, outcome)
    assert store.get_run(7).phase == step, "the premise: the last step survives finish_run"

    def working(*args, **kwargs):
        raise AssertionError("a finished run is never described by the live acorn explanation")

    for language in ("en", "ko"):
        shown = _shown(store, tmp_path, language, working)
        word = MESSAGES[language].get(f"issue_view_outcome_{outcome}", outcome)
        assert shown.count(text("issue_view_ended", language, outcome=word)) == 1, shown
        if step is not None:
            assert text("issue_view_state", language, state=step) not in shown


def test_a_new_live_run_is_described_by_its_own_step_again(tmp_path):
    """After a finished run, only an active new run brings back the live explanation."""
    from foragent.i18n import text

    store = _store(tmp_path)
    _step(store, "implementing")
    store.clock[0] += timedelta(minutes=1)
    store.finish_run(7, "crashed")
    live = []

    def explanation(target, *args, **kwargs):
        live.append(target)
        return [[("#7 live step", "")]]

    finished = _shown(store, tmp_path, "en", explanation)
    assert live == [] and text("issue_view_ended", "en", outcome="crashed") in finished
    store.clock[0] += timedelta(minutes=1)
    store.start_run(7, branch="foragent/issue-7", worktree=tmp_path / "wt")
    _step(store, "reviewing")
    shown = _shown(store, tmp_path, "en", explanation)
    assert live == [("acorn", 7)] and "#7 live step" in shown
    assert text("issue_view_ended", "en", outcome="crashed") not in shown


def test_finished_outcome_lines_never_say_the_lane_is_working():
    from foragent.messages.issue_view import MESSAGES

    for language, activity in (("en", ("again", "working", "retry")), ("ko", ("다시", "하고 있어요"))):
        for key, line in MESSAGES[language].items():
            if key.startswith("issue_view_outcome_") or key == "issue_view_ended":
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
