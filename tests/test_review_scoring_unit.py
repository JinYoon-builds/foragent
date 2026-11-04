"""#1651: a scoring session runs in the worktree, so its repository edits are undone like a reviewer's."""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "acceptance"))

from conftest import git_command  # noqa: E402
from foragent.runner import run_issue  # noqa: E402
from test_order_machine_budget import fake_workloads  # noqa: E402,F401
from test_review_grounded_fail import (  # noqa: E402
    GROUNDS, REAL, TASTE, UNGROUNDED, Script, install, ordinary, records, scoring_request,
)
from test_review_perspectives import review_offline  # noqa: E402,F401
from test_risk_map import entry, offline, risk_case  # noqa: E402,F401
from test_skills_autosync import isolated_home  # noqa: E402,F401

INVENTED = "tests/acceptance/test_scorer_invented.py"


class EditingScorer(Script):
    """A scorer that rewrites and stages the change, adds an untracked file and invents a locked test."""

    def run(self, **kwargs):
        if scoring_request(kwargs["prompt"]) is not None:
            worktree = kwargs["worktree"]
            (worktree / "feature.txt").write_text("scorer replacement\n")
            (worktree / "scorer-extra.txt").write_text("untracked\n")
            invented = worktree / INVENTED
            invented.parent.mkdir(parents=True, exist_ok=True)
            invented.write_text("def test_feature():\n    assert False\n")
            subprocess.run(["git", "add", "-A", "--", "feature.txt", INVENTED], cwd=worktree, check=True)
        return super().run(**kwargs)


def published(case, path):
    return git_command(case.history.origin, "show", f"foragent/issue-1:{path}")


def published_names(case):
    return git_command(case.history.origin, "ls-tree", "-r", "--name-only", "foragent/issue-1").splitlines()


def test_a_scorer_edit_is_undone_before_publication(git_history, tmp_path, monkeypatch):
    case, _ = ordinary(git_history, tmp_path, monkeypatch, [], UNGROUNDED)
    script = EditingScorer(case, [TASTE["1484-r1"]], UNGROUNDED)
    install(case, script)
    assert run_issue(1, case.ctx) == "merged"
    assert len(script.scorings) == 1
    assert published(case, "feature.txt") != "scorer replacement"
    names = published_names(case)
    assert "scorer-extra.txt" not in names and INVENTED not in names
    assert "Scorer changed repository state" in records(case)


def test_a_locked_test_the_scorer_wrote_is_not_a_ground(git_history, tmp_path, monkeypatch):
    case, _ = ordinary(git_history, tmp_path, monkeypatch, [], UNGROUNDED)
    reply = {**UNGROUNDED, "locked_test": f"{INVENTED}::test_feature"}
    script = EditingScorer(case, [TASTE["1484-r1"]], reply)
    install(case, script)
    assert run_issue(1, case.ctx) == "merged", "The invented file is gone before the fact check"
    assert INVENTED not in published_names(case)


@pytest.mark.parametrize("mapped", [True, False])
def test_panel_and_single_scoring_both_restore(git_history, tmp_path, monkeypatch, mapped):
    from foragent import run_review

    case = risk_case(git_history, tmp_path, monkeypatch,
                     entries=[entry(["feature.txt"] if mapped else ["docs/**"])],
                     **{"test_command": "true", "review_max": 0, "max_retries": 0})
    reviewers = 7 if mapped else 1
    script = EditingScorer(case, [REAL["1679-r1"]] * reviewers, {**UNGROUNDED, **GROUNDS["breaking_input"]})
    install(case, script)
    seen = []
    journal = run_review._journal_review

    def observe(n, ctx, *args, **kwargs):
        # The round is journaled right after its scoring: read the worktree the scorer edited.
        worktree = Path(script.scorings[0][1]["worktree"])
        seen.append(((worktree / "scorer-extra.txt").exists(), (worktree / INVENTED).exists(),
                     (worktree / "feature.txt").read_text(),
                     git_command(worktree, "diff", "--cached", "--name-only")))
        return journal(n, ctx, *args, **kwargs)

    monkeypatch.setattr(run_review, "_journal_review", observe)
    assert run_issue(1, case.ctx) == "held"
    assert len(script.scorings) == 1 and len(seen) == 1
    extra, invented, feature, staged = seen[0]
    assert not extra and not invented and feature != "scorer replacement\n"
    assert INVENTED not in staged.splitlines()
    assert "Scorer changed repository state" in records(case)
