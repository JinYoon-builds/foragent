"""Acceptance test for flush-ledger-from-git. Locked by Foragent; do not edit.

D109 (2026-11-04): git and GitHub are the source; Foragent's ledgers keep only
what they cannot know. A deferred publication batch's commit id and whether it
was pushed are Git facts (the commit carries its batch identity, and ancestry
in the remote base says whether it landed). Foragent keeps which orders and
notes went into a batch, never a copy of the commit id, and every interruption
still recovers exactly one batch commit from Git.

Real Git index and ancestry, real `fa order` CLI; the tracker is offline.
"""

from pathlib import Path

import pytest

from conftest import git_command
from foragent.git import GitError
from test_d85_batched_publication import BOUNDARY, call
from test_d85_flush_recovery import (  # noqa: F401
    Interrupted, commits, finish, publishing, queue_batch, recovery, stop,
)
from test_order import offline_publication, plan  # noqa: F401
from test_order_preread import preread, preread_case  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401


def saved_copies(sha):
    """Foragent's own files (the isolated home's data directory) that hold this commit id."""
    data = Path.home() / ".local" / "share" / "foragent"
    found = []
    for path in data.rglob("*"):
        if path.is_file() and "worktrees" not in path.parts:
            try:
                if sha in path.read_text(errors="ignore"):
                    found.append(str(path.relative_to(data)))
            except OSError:
                continue
    return found


def landed(case, sha):
    return sha in git_command(case.history.origin, "rev-list", "main").split()


def test_published_batch_keeps_no_commit_id(recovery, capsys):
    case = recovery
    values = queue_batch(case, capsys)
    finish(case, capsys, values)
    sha = commits(case)[0][3]
    assert landed(case, sha), "The batch reached the remote base"
    assert saved_copies(sha) == [], "Git knows the batch commit; Foragent keeps no copy of its id"


def test_failed_push_recovers_from_git_alone(recovery, capsys, monkeypatch):
    case = recovery
    values = queue_batch(case, capsys)

    def fail_push(branch):
        raise GitError("Injected push interruption")

    with monkeypatch.context() as patch:
        patch.setattr(case.git_adapter, "push_base", fail_push)
        call(case, capsys, "flush", "--boundary", BOUNDARY, ok=False)
    sha = commits(case)[0][3]
    assert saved_copies(sha) == [], "The unpushed batch commit is found in Git, not in a saved id"
    finish(case, capsys, values)  # Exactly one batch commit; the retry pushes it.
    assert landed(case, sha)
    assert saved_copies(sha) == []


@pytest.mark.parametrize("stage", ["after-commit", "after-push"])
def test_interruption_repeats_nothing_without_a_saved_commit_id(recovery, capsys, monkeypatch, stage):
    """Edge: the process stops right after the commit or right after the push."""
    case = recovery
    values = queue_batch(case, capsys)
    name = "commit_paths" if stage == "after-commit" else "push_base"
    original = getattr(case.git_adapter, name)

    def crash(*args):
        original(*args)
        raise Interrupted()

    with monkeypatch.context() as patch:
        patch.setattr(case.git_adapter, name, crash)
        stop(case, capsys)
    sha = commits(case)[0][3]
    finish(case, capsys, values)
    assert landed(case, sha), "The one batch commit lands"
    assert saved_copies(sha) == []
