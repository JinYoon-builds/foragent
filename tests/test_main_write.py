"""One admission point; remote facts are read again at every push attempt."""

import ast
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import git_command
from foragent import git as git_module, main_write
from foragent.config import Config
from foragent.git import GitError, GitRepo


def test_gate_reads_live_refs_under_the_existing_lock(git_history, monkeypatch):
    git = GitRepo(git_history.clone)
    config = Config(merge_owner=True)
    # Local tracking refs deliberately never see this remote train.
    train = f"refs/heads/foragent/batch-merge-{git_history.tip[:8]}-1"
    git_command(git_history.origin, "update-ref", train, git_history.tip)
    calls, pushed = [], []
    call = git._call

    def read(args):
        if args[0] == "ls-remote":
            assert any(git_module._MAIN_WRITES.held.values())
            calls.append(args)
        return call(args)

    def push(branch):
        assert any(git_module._MAIN_WRITES.held.values())
        pushed.append(branch)

    monkeypatch.setattr(git, "_call", read)
    monkeypatch.setattr(git, "push_base", push)
    with git.publish_lock():
        with pytest.raises(GitError, match="^waiting for merge train$"):
            main_write.push_base(git, config)
    assert not pushed
    git_command(git_history.origin, "update-ref", "-d", train)
    with git.publish_lock():
        main_write.push_base(git, config)
    assert pushed == ["main"]
    assert calls == [["ls-remote", "--heads", "origin", "refs/heads/foragent/batch-merge-*"]] * 2


def test_unreadable_train_refs_use_the_failed_push_path():
    pushed = []

    def unreadable(args):
        raise GitError("remote unavailable")

    git = SimpleNamespace(_call=unreadable, push_base=pushed.append)
    with pytest.raises(GitError, match="remote unavailable"):
        main_write.push_base(git, Config(merge_owner=True))
    assert not pushed


def test_owner_off_does_not_read_train_refs():
    # The legacy path needs only push_base, without an additional remote read.
    pushed = []
    main_write.push_base(SimpleNamespace(push_base=pushed.append), Config(base_branch="release/v1"))
    assert pushed == ["release/v1"]


def test_all_base_push_callers_use_the_one_gate():
    source = Path(main_write.__file__).parent
    direct = {path.name for path in source.rglob("*.py") if ".push_base(" in path.read_text()}
    assert direct == {"main_write.py"}
    writers = {path.name for path in source.rglob("*.py")
               if "from foragent.main_write import push_base" in path.read_text()}
    assert writers == {"orch_role_memory.py", "order.py", "order_flush_recovery.py", "lanes.py", "init.py"}
    # Inventory the lower-level escape hatches too: a new raw push must be audited.
    raw, branches = Counter(), Counter()
    for path in source.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if (node.func.attr == "_call" and node.args and isinstance(node.args[0], ast.List)
                    and node.args[0].elts and isinstance(node.args[0].elts[0], ast.Constant)
                    and node.args[0].elts[0].value == "push"):
                raw[path.name] += 1
            if node.func.attr in {"push", "push_head", "ensure_batch_branch"}:
                branches[path.name, node.func.attr] += 1
    assert raw == {"git.py": 4, "merge_owner_batch.py": 2, "run_group_ci.py": 2}
    assert branches == {
        ("orch_dream_publish.py", "push"): 1, ("run_publish_preserve.py", "push"): 1,
        ("run_publish_pr.py", "push"): 2, ("order_flush.py", "push"): 1,
        ("merge_owner.py", "push_head"): 1, ("run_publish_reviewed.py", "push_head"): 1,
        ("runner.py", "ensure_batch_branch"): 1, ("run_conflicts.py", "ensure_batch_branch"): 1,
    }
