"""Acceptance test for the single-issue dashboard. Locked by Foragent; do not edit.

A lane run is headless, so a person who opens its worktree sees no tab, only the raw
machine log. `fa dashboard <N> --once` (or `fa dashboard --once` inside the issue's lane
worktree) prints the dashboard focused on that one issue: its phase, the latest step,
the last review verdict and the CI state. It is read-only.

Wording and layout are the lane's: facts are checked by number and by the output changing
when only that fact changes, never by fixed words.
"""

import hashlib


def _run(main, argv):
    """Exit code of the real CLI; an unknown command (argparse) is a plain failing code, not an error."""
    try:
        return main(argv)
    except SystemExit as stop:
        return stop.code if isinstance(stop.code, int) else 1


def _case(git_history, tmp_path, monkeypatch):
    from datetime import timedelta

    from foragent.scheduler import tick
    from test_orch_review_handoff import tick_context
    from test_view_status import status_case

    case, main = status_case(git_history, tmp_path, monkeypatch)
    # Issue 1 runs in lane api; 2 is blocked, 3 waits for a person, 4 waits for #1.
    case.store.update_run(1, phase="ci",
                          phase_started_at=(case.clock[0] - timedelta(seconds=90)).isoformat())
    tick(tick_context(case, start_runs=False))
    return case, main


def _reviews(case, verdicts):
    """Rewrite issue 1's review journal as the runner writes it: one event per round, in order."""
    from datetime import timedelta

    from foragent import orch_events

    (case.store.root / "orch-events.jsonl").write_text("")
    for index, verdict in enumerate(verdicts, start=1):
        orch_events.append_event(case.store, case.clock[0] - timedelta(minutes=30 - index), 1, "review",
                                 f"{verdict} {0 if verdict == 'PASS' else 2} findings", verdict=verdict,
                                 findings=0 if verdict == "PASS" else 2, round=index, source="reviewer")


def _show(main, capsys, *argv):
    capsys.readouterr()
    code = _run(main, ["dashboard", *argv, "--once"])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _tree(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*")) if path.is_file()}


def test_dashboard_for_one_issue_shows_that_issue_with_phase_review_and_ci(
    git_history, tmp_path, monkeypatch, capsys,
):
    case, main = _case(git_history, tmp_path, monkeypatch)
    _reviews(case, ["FAIL", "PASS"])
    code, out, err = _show(main, capsys, "1")
    assert code == 0, f"`fa dashboard 1 --once` must print the dashboard for issue #1: {err}"
    assert "#1" in out and "Implement feature" in out
    for other, title in ((2, "Review dispute"), (3, "Choose locale"), (4, "Login page")):
        assert title not in out, f"the view is focused on #1; #{other} belongs to the full dashboard"
    assert "\x1b[" not in out, "one-shot output is plain text"

    assert _show(main, capsys, "1")[1] == out, "the same facts print the same view"

    # Phase: only the recorded phase changes, between two steps that are neither review nor CI.
    case.store.update_run(1, phase="implementing")
    implementing = _show(main, capsys, "1")[1]
    case.store.update_run(1, phase="checking")
    assert _show(main, capsys, "1")[1] != implementing, "the issue's current phase must be shown"
    case.store.update_run(1, phase="ci")

    # Last review verdict: the same first round, only the newest round's verdict differs.
    _reviews(case, ["FAIL", "FAIL"])
    assert _show(main, capsys, "1")[1] != out, "the last review verdict (not an earlier one) must be shown"


def test_dashboard_for_one_issue_shows_a_recorded_ci_failure(git_history, tmp_path, monkeypatch, capsys):
    case, main = _case(git_history, tmp_path, monkeypatch)
    _reviews(case, ["PASS"])
    # After a CI failure the runner returns the issue to implementing for another attempt.
    case.store.update_run(1, phase="implementing", attempts=2)
    code, before, err = _show(main, capsys, "1")
    assert code == 0, err
    run = case.store.get_run(1)
    case.store.record_flow("ci-failure", issue=1, run_started_at=run.started_at, pr=41, head="abc1234")
    after = _show(main, capsys, "1")[1]
    assert "#1" in after
    assert after != before, "the CI state of the issue's pull request (here: failed) must be shown"


def test_dashboard_inside_a_lane_worktree_needs_no_issue_number(git_history, tmp_path, monkeypatch, capsys):
    from conftest import git_command

    case, main = _case(git_history, tmp_path, monkeypatch)
    _reviews(case, ["PASS"])
    code, by_number, err = _show(main, capsys, "1")
    assert code == 0, err
    # The folder a person opens from Orca: the issue's lane worktree on its lane branch.
    worktree = case.store.worktree_path(1)
    worktree.parent.mkdir(parents=True, exist_ok=True)
    git_command(git_history.clone, "worktree", "add", "-q", "-b", "foragent/issue-1", str(worktree))
    monkeypatch.chdir(worktree)
    code, inferred, err = _show(main, capsys)
    assert code == 0, f"`fa dashboard --once` in issue #1's worktree must infer the issue: {err}"
    assert "#1" in inferred
    assert inferred == by_number, "inside the worktree the view is the same as `fa dashboard 1 --once`"


def test_dashboard_for_one_issue_is_read_only_and_names_an_unknown_issue(
    git_history, tmp_path, monkeypatch, capsys,
):
    case, main = _case(git_history, tmp_path, monkeypatch)
    _reviews(case, ["FAIL", "PASS"])
    state = _tree(case.store.root)
    tracker = (repr(sorted(case.tracker.issues.items())), repr(case.tracker.comments), len(case.tracker.created_prs))

    code, out, err = _show(main, capsys, "1")
    assert code == 0 and "#1" in out, err
    code, out, err = _show(main, capsys, "999")
    assert code != 0, "an issue Foragent knows nothing about is an error, not an empty view"
    assert "999" in out + err and "Traceback" not in out + err

    assert _tree(case.store.root) == state, "the single-issue dashboard writes no local state"
    assert (repr(sorted(case.tracker.issues.items())), repr(case.tracker.comments),
            len(case.tracker.created_prs)) == tracker, "the single-issue dashboard writes nothing to GitHub"
