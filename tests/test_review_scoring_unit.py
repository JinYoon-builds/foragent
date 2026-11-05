"""#1651: a scoring session runs in the worktree, so its repository edits are undone like a reviewer's."""

import subprocess
import sys
import json
from pathlib import Path

import pytest

from conftest import git_command
from foragent.runner import run_issue
from helpers.workloads import fake_workloads  # noqa: F401
from helpers.review_scoring import (
    GROUNDS, REAL, TASTE, UNGROUNDED, Script, install, ordinary, records, scoring_request,
)
from helpers.review_panel import review_offline  # noqa: F401
from helpers.risk import entry, offline, risk_case  # noqa: F401
from helpers.home import isolated_home  # noqa: F401

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


@pytest.mark.parametrize("exists,wrong,ordinary,irreversible,expected", [
    (True, True, True, False, "blocks"),
    (True, True, False, False, "note"),
    (True, False, True, False, "note"),
    (True, False, False, False, "note"),
    (True, True, True, True, "blocks"),
    (True, True, False, True, "blocks"),
    (True, False, True, True, "blocks"),
    (True, False, False, True, "blocks"),
    (False, True, True, True, "note"),
    (False, True, True, False, "note"),
])
@pytest.mark.parametrize("critical", [False, True])
def test_consequence_facts_decide_blocking(exists, wrong, ordinary, irreversible, expected, critical):
    from foragent.review_scoring import outcome

    reply = {**UNGROUNDED, "exists": exists, "wrong_result": wrong, "ordinary_input": ordinary,
             "irreversible": irreversible, "critical_path": critical}
    assert outcome(reply, ["breaking_input"]) == expected
    assert outcome(reply, []) == "note"


@pytest.mark.parametrize("field", ["wrong_result", "ordinary_input", "irreversible", "finding_class"])
@pytest.mark.parametrize("value", [None, 0, "false", []])
def test_invalid_consequence_facts_never_authorize_a_pass(field, value):
    from foragent.review_scoring import parse_reply

    # A string is valid only for the class name; remove it to exercise a missing field instead.
    reply = {**UNGROUNDED, field: value}
    if field == "finding_class" and value == "false":
        reply.pop(field)
    assert parse_reply(json.dumps(reply)) is None


def test_repeated_classes_count_distinct_failed_rounds_in_the_approved_scope(tmp_path):
    from datetime import UTC, datetime
    from types import SimpleNamespace
    from foragent import orch_events, review_scoring
    from foragent.state import StateStore

    store = StateStore(tmp_path / "state")
    store.save_approval(1, body="scope", body_sha="current", acceptance_blobs={},
                        approved_by="human", approved_at=datetime.now(UTC).isoformat())
    ctx = SimpleNamespace(store=store)

    def record(issue, number, classes, verdict="FAIL", body="current"):
        orch_events.append_event(store, datetime.now(UTC), issue, "review", verdict,
                                 round=number, verdict=verdict, finding_classes=classes, body_sha=body)

    record(2, 1, ["A"])
    record(1, 1, ["A"], body="old")
    record(1, 2, ["A", "A"])
    record(1, 2, ["A"])
    record(1, 3, ["B"])
    assert review_scoring.repeated_classes(1, ctx) == []
    record(1, 4, ["A"])
    assert review_scoring.repeated_classes(1, ctx) == ["A"]
    record(1, 5, [], verdict="PASS")
    record(1, 6, ["A"])
    assert review_scoring.repeated_classes(1, ctx) == []


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
