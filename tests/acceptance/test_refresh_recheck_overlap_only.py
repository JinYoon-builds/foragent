"""Acceptance tests for refresh-recheck-overlap-only.

After a base refresh (main moved after the reviewer PASS), the local check runs
again only the related unit tests of files that main changed since the last
check and that this issue also touches; the rest is left to PR CI. Several
refreshes in one run must work. CI mode off keeps today's behaviour (every
refresh runs all related tests).

Observations are refreshed heads (what origin's issue branch carries), the
run outcome, CI evidence reads and the tests each local pytest call executed
(JUnit), never log wording.

Real runner, Git, pytest selection and JUnit reports; only GitHub, models and
the clock are fake (the ci-mode world of test_ci_mode, see
refresh_overlap_world).
"""

from pathlib import Path

from foragent import runner
from test_ci_mode import (  # noqa: F401 -- offline fixture
    LOCAL, LOCAL_ACCEPTANCE, OWN_ACCEPTANCE, no_external_execution,
)
from test_skills_autosync import isolated_home  # noqa: F401
from test_state_split import offline_acceptance  # noqa: F401

import refresh_overlap_world as world
from refresh_overlap_world import DEMO_TEST, EXTRA_TEST, RELATED


def run_ci_world(git_history, tmp_path, monkeypatch, first, second=None):
    root = tmp_path / "ci-mode"
    root.mkdir()
    case = world.build_case(git_history, root, monkeypatch)
    world.schedule_moves(case, first, second)
    outcome = runner.run_issue(1, case.ctx)
    return case, outcome


def log_of(case):
    """The run log's last lines, for failure messages."""
    return "\n".join(case.store.log_path(1).read_text().splitlines()[-25:])


def assert_merged_carrying(case, outcome, *moves):
    """The run merged, and the pushed head carries the issue's change and every move."""
    assert outcome == "merged", log_of(case)
    assert case.tracker.merged_prs == [2]
    head = world.pushed_head(case)
    assert head == case.tracker.pr_head_sha(2)
    demo = world.pushed_file(case, "src/demo.py").splitlines()
    assert demo[0] == "VALUE = 2", "The pushed head keeps the issue's own change"
    assert world.pushed_file(case, "src/extra.py") == "EXTRA = 2"
    for move in moves:
        assert world.carries(case, head, move), f"The pushed head must carry main's move {move}"
    return head


def related_runs(case, start, end=None):
    """(local head runs in the window, how many calls executed each related test)."""
    calls = world.head_runs(case, case.processes.calls[start:end])
    counts = {test: sum(test in call.executed for call in calls) for test in RELATED}
    return calls, counts


def assert_refresh_rechecked(calls, label):
    """A refresh still re-verifies on this machine (Done when, local_only units)."""
    ran = world.executed(calls)
    assert calls, f"{label}: the refresh must re-verify on this machine"
    assert OWN_ACCEPTANCE in ran, f"{label}: the refresh still runs Done when on the merged head; ran {sorted(ran)}"
    assert LOCAL in ran, f"{label}: the refresh still runs the local_only unit tests; ran {sorted(ran)}"
    assert LOCAL_ACCEPTANCE in ran, f"{label}: the refresh still runs the protected local_only acceptance"


def no_related_file_started(calls):
    names = {Path(test.split("::")[0]).name for test in RELATED}
    return not any(Path(word.split("::")[0]).name in names for call in calls for word in call.words)


# Statement 1 -------------------------------------------------------------


def test_later_refresh_that_changes_no_issue_file_runs_no_related_tests(git_history, tmp_path, monkeypatch):
    """Two post-PASS refreshes in CI mode: the first brings a change to src/demo.py
    (an issue file), the second only src/neighbor.py. Since the last check main
    changed no issue file, so the second refresh runs no related tests again,
    yet it still re-verifies and the run merges the twice-refreshed head."""
    first, second = world.demo_move("one"), world.neighbor_move("two")
    case, outcome = run_ci_world(git_history, tmp_path, monkeypatch, first, second)

    assert "second" in case.marks, f"Main must move a second time during the CI wait; {log_of(case)}"
    assert_merged_carrying(case, outcome, first, second)
    assert world.carries(case, case.marks["first_head"], first)
    assert not world.carries(case, case.marks["first_head"], second)

    before, before_counts = related_runs(case, 0, case.marks["first"])
    assert before_counts[DEMO_TEST] and before_counts[EXTRA_TEST], "The first check runs all related tests"
    after_second, counts = related_runs(case, case.marks["second"])
    assert_refresh_rechecked(after_second, "second refresh")
    assert counts == {DEMO_TEST: 0, EXTRA_TEST: 0}, (
        f"Main changed no issue file since the last check: no related test may run again; observed {counts}"
    )
    assert no_related_file_started(after_second), "No related unit file may be started locally"


def test_single_refresh_that_changes_no_issue_file_runs_no_related_tests(git_history, tmp_path, monkeypatch):
    """One post-PASS refresh that brings only src/neighbor.py, with two refreshes
    configured so the run must also survive the second identical-kind move."""
    first, second = world.neighbor_move("one"), world.neighbor_move("two")
    case, outcome = run_ci_world(git_history, tmp_path, monkeypatch, first, second)

    assert "second" in case.marks, log_of(case)
    head = assert_merged_carrying(case, outcome, second)
    # The second move replaces the first value of src/neighbor.py: the earlier
    # move is checked on the intermediate pushed head and by commit ancestry.
    assert world.carries(case, case.marks["first_head"], first), "The first refresh pushed main's first move"
    assert head != case.marks["first_head"], "The second refresh pushed a new head"
    assert world.is_ancestor(case, case.marks["first_tip"], head), "The final head contains main's first move"
    assert world.is_ancestor(case, case.marks["second_tip"], head), "The final head contains main's second move"
    for label, start, end in (("first refresh", case.marks["first"], case.marks["second"]),
                              ("second refresh", case.marks["second"], None)):
        calls, counts = related_runs(case, start, end)
        assert_refresh_rechecked(calls, label)
        assert counts == {DEMO_TEST: 0, EXTRA_TEST: 0}, f"{label}: no related test may run; observed {counts}"


# Statement 2 -------------------------------------------------------------


def test_refresh_that_changes_an_issue_file_runs_that_files_related_tests(git_history, tmp_path, monkeypatch):
    """One post-PASS refresh brings a change to src/demo.py, which the issue also
    changes: the local check runs tests/test_demo.py once, and leaves
    tests/test_extra.py (whose source main did not touch) to PR CI."""
    first = world.demo_move("one")
    case, outcome = run_ci_world(git_history, tmp_path, monkeypatch, first)

    assert_merged_carrying(case, outcome, first)
    after, counts = related_runs(case, case.marks["first"])
    assert_refresh_rechecked(after, "refresh")
    assert counts[DEMO_TEST] == 1, (
        f"Main changed src/demo.py, which this issue touches: run its related test once; observed {counts}"
    )
    assert counts[EXTRA_TEST] == 0, (
        f"Main did not change src/extra.py: its related test stays with PR CI; observed {counts}"
    )
    head = case.tracker.pr_head_sha(2)
    assert any(read_head == head for read_head, _ in case.ci.reads), (
        "Merge still needs CI evidence for the exact pushed head"
    )


def test_later_refresh_that_changes_an_issue_file_runs_its_related_tests(git_history, tmp_path, monkeypatch):
    """First refresh brings only src/neighbor.py (no related tests), the second
    brings a change to src/demo.py: the second runs tests/test_demo.py only."""
    first, second = world.neighbor_move("one"), world.demo_move("two")
    case, outcome = run_ci_world(git_history, tmp_path, monkeypatch, first, second)

    assert "second" in case.marks, log_of(case)
    assert_merged_carrying(case, outcome, first, second)
    _, first_counts = related_runs(case, case.marks["first"], case.marks["second"])
    assert first_counts == {DEMO_TEST: 0, EXTRA_TEST: 0}, f"first refresh: observed {first_counts}"
    after_second, counts = related_runs(case, case.marks["second"])
    assert_refresh_rechecked(after_second, "second refresh")
    assert counts == {DEMO_TEST: 1, EXTRA_TEST: 0}, (
        f"Since the last check main changed src/demo.py only: run tests/test_demo.py once; observed {counts}"
    )


# CI mode off keeps today's behaviour (guard, PM answers 3) -----------------


def test_ci_mode_off_refresh_still_runs_all_related_tests(git_history, tmp_path, monkeypatch):
    first = world.neighbor_move("one")
    case, outcome = world.local_mode_world(git_history, tmp_path, first)

    assert_merged_carrying(case, outcome, first)
    after, counts = related_runs(case, case.marks["first"])
    assert after, "The refresh re-verifies on this machine"
    assert counts[DEMO_TEST] >= 1 and counts[EXTRA_TEST] >= 1, (
        f"With CI mode off a refresh still runs every related test locally; observed {counts}"
    )
