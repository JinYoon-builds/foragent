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


@pytest.mark.parametrize("listing, expected", [
    ({"queued": [{"number": 7}]}, "queued"),
    ({"running": [{"number": 7}]}, "working"),
    ({"held": [{"number": 7, "state": "blocked"}]}, "blocked"),
    ({"held": [{"number": 7, "state": "waiting-human"}]}, "waiting-human"),
    ({"held": [{"number": 7, "state": "waiting-merge"}]}, "waiting-merge"),
    ({"issue": 7, "state": "waiting-human"}, "waiting-human"),
    ({"queued": [{"number": 8}]}, None),
])
def test_a_finished_run_leads_with_the_main_dashboards_state(tmp_path, listing, expected):
    """The state line comes from the status lane rows; the run's outcome stays under it as history."""
    from types import SimpleNamespace

    from foragent.config import Config
    from foragent.i18n import text

    store = _store(tmp_path)
    store.finish_run(7, "held")
    snapshot = SimpleNamespace(status={"lanes": {"docs": {"state": "idle", **listing}}})
    assert watch_issue.current_state(snapshot, store, 7) == expected
    for language in ("en", "ko"):
        lines = watch_issue.plain(watch_issue.rows(snapshot, store, Config(language=language), tmp_path, 7,
                                                   now=store.clock[0], width=120)).splitlines()
        now = text(f"issue_view_now_{expected or 'none'}", language)
        ended = text("issue_view_ended", language, outcome=text("issue_view_outcome_held", language))
        assert lines.count(now) == 1 and lines.index(now) < lines.index(ended), lines


def test_a_closed_issue_reads_merged_only_when_its_pr_merged(tmp_path):
    store = _store(tmp_path)
    store.finish_run(7, "merge-queued")
    rows = [{"number": 7, "state": "closed", "merged": True}]
    store.local_issues = lambda: rows
    assert watch_issue.current_state(None, store, 7) == "merged"
    rows[0]["merged"] = False
    assert watch_issue.current_state(None, store, 7) == "closed"


def test_every_state_line_has_words_in_both_languages():
    from foragent.messages.issue_view import MESSAGES

    for language in ("en", "ko"):
        missing = [state for state in watch_issue.NOW_STATES
                   if f"issue_view_now_{state}" not in MESSAGES[language]]
        assert missing == [], (language, missing)


def test_every_state_the_producers_return_is_in_the_catalogue():
    """status.issue_state for every label mix, plus the dashboard's merge-owner turn."""
    from itertools import combinations

    from foragent import status

    labels = ["needs-human", "waiting-person", "blocked", "running", "approved", "ready"]
    seen = {status.issue_state(set(mix), active, group)
            for size in range(len(labels) + 1) for mix in combinations(labels, size)
            for active in (False, True) for group in (None, "pr-open", "running")}
    seen.discard(None)
    seen.add("waiting-merge")  # watch_data.merge_turns
    assert seen - set(watch_issue.NOW_STATES) == set()


def test_a_finished_run_whose_pr_waits_for_the_merge_owner(tmp_path):
    """The review's crash: merge_turns turns the held PR into waiting-merge."""
    from types import SimpleNamespace

    from foragent.config import Config
    from foragent.i18n import text
    from foragent.watch_data import merge_turns

    store = _store(tmp_path)
    store.finish_run(7, "merge-queued")
    status = {"issues": {"pr-open": [{"number": 7}]},
              "lanes": {"docs": {"state": "waiting-human", "issue": 7,
                                 "held": [{"number": 7, "state": "waiting-human"}]}}}
    turned = merge_turns(status, {7: {"labels": []}}, SimpleNamespace(merge_owner=True))
    snapshot = SimpleNamespace(status=turned)
    for language in ("en", "ko"):
        shown = watch_issue.plain(watch_issue.rows(snapshot, store, Config(language=language), tmp_path, 7,
                                                   now=store.clock[0], width=120)).splitlines()
        assert shown[1] == text("issue_view_now_waiting-merge", language), shown


def test_a_state_without_words_reads_as_the_plain_state():
    from foragent.i18n import text

    for language in ("en", "ko"):
        assert watch_issue.now_line("parked-somewhere", language) == text(
            "issue_view_state", language, state="parked-somewhere")
        assert watch_issue.now_line(None, language) == text("issue_view_now_none", language)


def _reason(facts, language="en"):
    from types import SimpleNamespace

    from foragent import watch_scene

    snapshot = SimpleNamespace(waiting_facts={4: facts}, meter={})
    return watch_scene.waiting_reason(snapshot, {"issue": None, "queued": []}, 4, language)


@pytest.mark.parametrize("language", ["en", "ko"])
def test_a_dependency_and_an_overlap_with_another_peer_name_each_peer(language):
    """#4 waits after #3 and shares work/z.py with #1: the file is attributed to #1 only."""
    reason = _reason({"kind": "dependency", "blocking_issues": [3], "overlapping_files": ["work/z.py"],
                      "overlap_peers": {"work/z.py": [1]}}, language)
    after, _, shared = reason.partition("#1")
    assert "#3" in after and "work/z.py" not in after, reason
    assert "work/z.py" in shared and "#3" not in shared, reason


def test_shared_paths_are_grouped_by_their_own_peers():
    reason = _reason({"kind": "file-overlap", "blocking_issues": [],
                      "overlapping_files": ["src/a.py", "src/b.py", "src/c.py"],
                      "overlap_peers": {"src/a.py": [1], "src/b.py": [2], "src/c.py": [1]}})
    assert reason == "waiting its turn · file overlap with #1: src/a.py, src/c.py; file overlap with #2: src/b.py"


def test_unattributed_shared_paths_keep_the_plain_overlap_sentence():
    facts = {"kind": "file-overlap", "blocking_issues": [], "overlapping_files": ["src/a.py"]}
    assert _reason(facts) == "waiting its turn · file overlap: src/a.py"
    assert _reason(dict(facts, overlap_peers={"src/a.py": []})) == "waiting its turn · file overlap: src/a.py"


def test_declared_files_come_from_each_open_body_and_none_reads_as_empty():
    from foragent import watch_data

    rows = {1: {"state": "open", "body": "## Files\n- work/a.py\n- work/**\n"},
            2: {"state": "open", "body": "## Goal\nNo files.\n"},
            3: {"state": "closed", "body": "## Files\n- x.py\n"}}
    assert watch_data.declared_files(rows) == {1: ["work/a.py", "work/**"], 2: []}
    assert watch_data.declared_files(rows, {2}) == {2: []}
