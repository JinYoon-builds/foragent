"""Locked acceptance for group-reuse. Do not edit during implementation.

Break-it: rechecking every follower, a leader merging peers, sorting by issue
number, using ordinary ancestry merges for a squash result, or publishing a
different tree all fail. No group event schema or private group API is required.
"""

from collections import Counter
import json
from pathlib import Path
import shlex

import pytest

from conftest import git_command
from foragent import runner
from test_merge_group import (  # noqa: F401
    contend, group_case, isolated_home, offline_acceptance, prepare, publish_case,
)


def observe_checked_trees(case, monkeypatch):
    checked = []
    run = runner.run_logged

    def observe(argv, *, cwd, **kwargs):
        root = Path(cwd)
        if (case.armed and not case.merges and "tools/group_probe.py" in argv[-1]
                and all((root / f"src/member_{n}.py").exists() for n in case.members)):
            files = git_command(root, "ls-files", "-z").split("\0")
            checked.append({name: git_command(root, "hash-object", "--", name)
                            for name in files if name})
        return run(argv, cwd=cwd, **kwargs)

    monkeypatch.setattr(runner, "run_logged", observe)
    return checked


def published_tree(case):
    result = {}
    for entry in git_command(case.history.origin, "ls-tree", "-r", "-z", "main").split("\0"):
        if entry:
            metadata, name = entry.split("\t", 1)
            result[name] = metadata.split()[2]
    return result


def assert_own_effects(case):
    assert all(row.issue == int(next(pr.head for pr in case.tracker.created_prs
                                    if pr.number == row.pr).rsplit("-", 1)[1])
               for row in case.merges), "Only a PR's own runner may merge it"
    assert all(actor == n for actor, n in case.closes), "Only the issue owner may close it"
    assert all(actor == n for actor, n in case.record_writes), "Run records have one writer"
    assert all(actor == n for actor, n, _ in case.board_writes), "Board state has one writer"


@pytest.mark.parametrize("neutral_move", [False, True], ids=["only-members", "neutral-move"])
def test_exact_heads_reuse_one_pass_and_publish_the_checked_tree(group_case, monkeypatch, neutral_move):
    case = group_case
    prepare(case, (5, 1, 4))
    checked = observe_checked_trees(case, monkeypatch)

    def move_main(n):
        if neutral_move and n == 5:
            with case.external():
                case.history.update({"docs/neutral-group.md": "Independent notes\n"})

    case.on_merged = move_main
    contend(case)
    assert checked, "Missing joint tree: no combined ready-head check was executed"
    assert all(tree == checked[0] for tree in checked), "One suite must check one stable combined tree"
    done = [row for row in case.probes if row.armed and "tools/group_probe.py" in row.command]
    assert Counter(int(shlex.split(row.command)[-1]) for row in done) == Counter(case.members), (
        "Earlier covered merges and neutral main moves must not trigger follower rechecks"
    )
    assert [row.issue for row in case.merges] == list(case.members)
    ready_heads = {row["n"]: row["head_sha"] for _, row in case.appends if row["event"] == "ready"}
    assert all(row.head == ready_heads[row.issue] for row in case.merges), "Merge exactly the checked PR heads"
    actual = published_tree(case)
    if neutral_move:
        assert actual.pop("docs/neutral-group.md", None), "Keep the permitted neutral move"
    assert actual == checked[0], "The sequential PR squash result must equal the checked combined tree"
    assert all(case.outcomes[n] == "merged" and case.tracker.get_issue(n).state == "closed"
               for n in case.members)
    assert len(case.merges) == len(case.members), "Never publish a member twice"
    assert_own_effects(case)


def test_later_head_containing_and_reverting_a_peer_still_checks_the_squash_result(group_case, monkeypatch):
    case = group_case
    prepare(case, (1, 4))
    implement = case.implementer.steps[0]

    def implement_with_reverted_ancestry(kwargs):
        implement(kwargs)
        if case.worker.number != 4:
            return
        root = kwargs["worktree"]
        # Fixture history: PR 4 descends from PR 1 but has reverted its files.
        # Import the current staged contents with PR 1 as parent, using no
        # commit/push/checkout/reset/stash command.
        parent = git_command(root, "rev-parse", "foragent/issue-1")
        branch = git_command(root, "symbolic-ref", "HEAD")
        git_command(root, "add", "src/member_4.py", "tests/test_member_4.py")
        stream = [f"commit {branch}\n", "committer Tests <test@example.com> 1792483200 +0000\n",
                  "data 7\nRevert\n\n", f"from {parent}\n", "deleteall\n"]
        for entry in git_command(root, "ls-files", "--stage", "-z").split("\0"):
            if entry:
                metadata, name = entry.split("\t", 1)
                mode, sha, stage = metadata.split()
                assert stage == "0"
                stream.append(f"M {mode} {sha} {json.dumps(name)}\n")
        git_command(root, "fast-import", "--quiet", "--force", input="".join(stream) + "\ndone\n")
        git_command(root, "read-tree", "--reset", "-u", "HEAD")
        assert not (root / "src/member_1.py").exists()
        git_command(root, "merge-base", "--is-ancestor", parent, "HEAD")

    case.implementer.steps = [implement_with_reverted_ancestry] * 2
    checked = observe_checked_trees(case, monkeypatch)
    contend(case)
    assert checked, "The checked squash union must retain both members despite the later head's ancestry"
    assert [row.issue for row in case.merges] == [1, 4]
    actual = published_tree(case)
    assert "src/member_1.py" in actual and "src/member_4.py" in actual
    assert all(tree == actual for tree in checked), "An ordinary ancestry merge checks the wrong tree here"
    done = [row for row in case.probes if row.armed and "tools/group_probe.py" in row.command]
    assert Counter(int(shlex.split(row.command)[-1]) for row in done) == Counter((1, 4))
    assert all(case.outcomes[n] == "merged" for n in (1, 4))
    assert_own_effects(case)
