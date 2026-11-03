"""Acceptance test for orch-scorecard-revert-facts. Locked by Foragent; do not edit."""

from dataclasses import replace
from datetime import timedelta
import json

from conftest import git_command
from test_local_truth import record_gh
from test_orch_review_handoff import bind_cli
from test_orch_scorecard import NOW, scorecard_case
from test_skills_autosync import cli_exit


def seeded_revert_case(git_history, tmp_path, monkeypatch, *, reverted_at):
    """Import an actual change and its canonical revert into the default branch.

    Git fast-import makes the history offline without git commit or git revert.
    A non-default branch also has a revert in the window, and is checked out.
    """
    case = scorecard_case(git_history, tmp_path, monkeypatch)
    case.ctx.config = replace(case.ctx.config, base_branch="trunk")
    history = case.history
    history.update({"feature.txt": "enabled\n"}, at=NOW - timedelta(days=3), message="Enable feature")
    changed = history.tip
    history.update({"feature.txt": None}, at=reverted_at,
                   message=f'Revert "Enable feature"\n\nThis reverts commit {changed}.')
    default_tip = history.tip
    git_command(history.origin, "update-ref", "refs/heads/trunk", default_tip)
    git_command(history.origin, "symbolic-ref", "HEAD", "refs/heads/trunk")
    history.update({"branch-only.txt": "enabled\n"}, at=NOW - timedelta(hours=2), message="Branch only")
    changed = history.tip
    history.update({"branch-only.txt": None}, at=NOW - timedelta(hours=1),
                   message=f'Revert "Branch only"\n\nThis reverts commit {changed}.')
    git_command(history.origin, "update-ref", "refs/heads/feature/decoy", history.tip)
    case.git.fetch()
    git_command(case.root, "update-ref", "refs/heads/trunk", default_tip)
    git_command(case.root, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
    git_command(case.root, "update-ref", "refs/heads/feature/decoy", history.tip)
    git_command(case.root, "symbolic-ref", "HEAD", "refs/heads/feature/decoy")
    return case, bind_cli(case, monkeypatch)


def read_card(case, main, capsys):
    # Reading facts must not create a revert journal, cache or other state.
    before = {path.relative_to(case.store.root): path.read_bytes()
              for path in case.store.root.rglob("*") if path.is_file()}
    assert cli_exit(main, ["orch", "scorecard", "--days", "2", "--json"]) == 0
    card = json.loads(capsys.readouterr().out)
    after = {path.relative_to(case.store.root): path.read_bytes()
             for path in case.store.root.rglob("*") if path.is_file()}
    assert after == before, "Scorecard observation must remain read-only"
    return card


def test_scorecard_counts_revert_commits_on_default_branch_without_events(
    git_history, tmp_path, monkeypatch, capsys,
):
    case, main = seeded_revert_case(git_history, tmp_path, monkeypatch,
                                   reverted_at=NOW - timedelta(hours=4))
    calls = record_gh(monkeypatch)
    card = read_card(case, main, capsys)
    assert card["window"]["today"]["reverts_after_merge"] == 1
    assert card["window"]["previous"]["reverts_after_merge"] == 0
    assert calls == []


def test_scorecard_moves_a_revert_out_of_the_window_by_its_commit_time(
    git_history, tmp_path, monkeypatch, capsys,
):
    case, main = seeded_revert_case(git_history, tmp_path, monkeypatch,
                                   reverted_at=NOW - timedelta(hours=4))
    calls = record_gh(monkeypatch)
    assert read_card(case, main, capsys)["window"]["today"]["reverts_after_merge"] == 1
    case.clock[0] = NOW + timedelta(days=1)
    card = read_card(case, main, capsys)
    assert card["window"]["today"]["reverts_after_merge"] == 0
    assert card["window"]["previous"]["reverts_after_merge"] == 1
    case.clock[0] = NOW + timedelta(days=2)
    card = read_card(case, main, capsys)
    assert card["window"]["today"]["reverts_after_merge"] == 0
    assert card["window"]["previous"]["reverts_after_merge"] == 0
    assert calls == []


def test_scorecard_reports_unreadable_default_branch_history_as_unknown(
    git_history, tmp_path, monkeypatch, capsys,
):
    case, main = seeded_revert_case(git_history, tmp_path, monkeypatch,
                                   reverted_at=NOW - timedelta(hours=4))
    calls = record_gh(monkeypatch)
    # Filesystem failure at the Git boundary, independent of which Git adapter
    # the lane chooses. Keep the configured repo and local state readable.
    (case.root / ".git").rename(case.root / ".git-unavailable")
    # Never let Git discover the checkout that contains pytest's temp files.
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(case.root.parent))
    card = read_card(case, main, capsys)
    for metrics in card["window"].values():
        assert metrics["reverts_after_merge"] is None
    assert "reverts_after_merge" not in card["worse"]
    assert cli_exit(main, ["orch", "scorecard"]) == 0
    lines = capsys.readouterr().out.casefold().splitlines()
    assert any("revert" in line and "unknown" in line for line in lines), lines
    assert calls == []
