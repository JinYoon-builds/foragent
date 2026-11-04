"""Retryable writers inspect live train refs at every push destination."""

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
    assert calls == [["ls-remote", "--heads", str(git_history.origin),
                      "refs/heads/foragent/batch-merge-*"]] * 2


@pytest.mark.parametrize("train_at", ["fetch", "first-push", "second-push", None])
def test_gate_inspects_every_push_destination(git_history, tmp_path, monkeypatch, train_at):
    git = GitRepo(git_history.clone)
    destinations = {"fetch": git_history.origin}
    for name in ("first-push", "second-push"):
        destination = tmp_path / f"{name}.git"
        git_command(tmp_path, "clone", "--bare", str(git_history.origin), str(destination))
        git_command(git_history.clone, "config", "--add", "remote.origin.pushurl", str(destination))
        destinations[name] = destination
    if train_at:
        git_command(destinations[train_at], "update-ref",
                    f"refs/heads/foragent/batch-merge-{git_history.tip[:8]}-1", git_history.tip)
    pushed = []
    monkeypatch.setattr(git, "push_base", pushed.append)
    with git.publish_lock():
        if train_at in {"first-push", "second-push"}:
            with pytest.raises(GitError, match="waiting for merge train"):
                main_write.push_base(git, Config(merge_owner=True))
            assert not pushed
        else:
            main_write.push_base(git, Config(merge_owner=True))
            assert pushed == ["main"]


def test_gate_inspects_the_rewritten_push_url(git_history, tmp_path, monkeypatch):
    destination = tmp_path / "push.git"
    git_command(tmp_path, "clone", "--bare", str(git_history.origin), str(destination))
    git_command(git_history.clone, "config", f"url.{destination}.pushInsteadOf", str(git_history.origin))
    git_command(destination, "update-ref",
                f"refs/heads/foragent/batch-merge-{git_history.tip[:8]}-1", git_history.tip)
    git = GitRepo(git_history.clone)
    pushed = []
    monkeypatch.setattr(git, "push_base", pushed.append)
    with git.publish_lock(), pytest.raises(GitError, match="waiting for merge train"):
        main_write.push_base(git, Config(merge_owner=True))
    assert not pushed


@pytest.mark.parametrize("failure", ["resolve", "list"])
def test_unreadable_train_refs_use_the_failed_push_path(failure):
    pushed = []

    def unreadable(args):
        if args[0] == "remote" and failure == "list":
            return "/unavailable/push.git\n"
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


def test_publication_owner_off_does_not_read_train_refs_or_wait(monkeypatch):
    def unexpected():
        pytest.fail("owner-off publication must not start a wait")

    monkeypatch.setattr(main_write.time, "monotonic", unexpected)
    main_write.wait_for_merge_train(SimpleNamespace(), Config())


def test_publication_wait_progress_is_written_to_stderr(monkeypatch, capsys):
    polls, sleeps = iter([True, False]), []
    monkeypatch.setattr(main_write, "train_in_flight", lambda git: next(polls))
    monkeypatch.setattr(main_write.time, "monotonic", lambda: 0)
    monkeypatch.setattr(main_write.time, "sleep", sleeps.append)

    main_write.wait_for_merge_train(SimpleNamespace(), Config(merge_owner=True))

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "waiting for merge train (0 min)\n"
    assert sleeps == [30]


def test_publication_wait_bounds_the_final_poll(monkeypatch):
    elapsed, sleeps = [0], []
    monkeypatch.setattr(main_write.time, "monotonic", lambda: elapsed[0])

    def in_flight(git):
        # Ref inspection consumes part of the last polling interval.
        elapsed[0] += 40 * 60 - 5
        return True

    def sleep(seconds):
        sleeps.append(seconds)
        elapsed[0] += seconds

    monkeypatch.setattr(main_write, "train_in_flight", in_flight)
    monkeypatch.setattr(main_write.time, "sleep", sleep)
    with pytest.raises(GitError, match="timed out after 40 minutes"):
        main_write.wait_for_merge_train(SimpleNamespace(), Config(merge_owner=True))
    assert sleeps == [5]


def test_only_retryable_base_writers_use_the_train_gate():
    source = Path(main_write.__file__).parent
    direct = {path.name for path in source.rglob("*.py") if ".push_base(" in path.read_text()}
    assert direct == {"main_write.py", "order.py", "init.py", "lanes.py"}
    writers = {path.name for path in source.rglob("*.py")
               if "from foragent.main_write import push_base" in path.read_text()}
    assert writers == {"orch_role_memory.py", "order_flush_recovery.py"}
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
    # The owner creates/deletes train refs and lands their tip with a target lease.
    # Explicit closure pushes its private commit under main_write_lock with an exact SHA lease.
    assert raw == {"git.py": 4, "merge_owner_batch.py": 3, "run_group_ci.py": 2, "orch_close.py": 1}
    assert branches == {
        ("orch_dream_publish.py", "push"): 1, ("run_publish_preserve.py", "push"): 1,
        # #1985: first publish and rebuild share run_publish_pr.push_publication (one leased push).
        ("run_publish_pr.py", "push"): 1, ("order_flush.py", "push"): 1,
        ("merge_owner.py", "push_head"): 1, ("run_publish_reviewed.py", "push_head"): 1,
        ("runner.py", "ensure_batch_branch"): 1, ("run_conflicts.py", "ensure_batch_branch"): 1,
    }
