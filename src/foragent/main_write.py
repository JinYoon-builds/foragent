"""Merge-train admission for base writers.

Publication waits leave the shared main-write lock available to the owner.
The owner can still create a train between the ref lookup and the push.
"""

import sys
import time
from pathlib import PurePosixPath

from foragent.git import GitError
from foragent.run_base_move import _git_bytes, _moved_changes, _nul_paths


def changed_paths(git, before, after):
    return set(_nul_paths(_git_bytes(git, [
        "diff", "--name-only", "--no-renames", "-z", before, after,
    ])))


def changed_inputs(git, before, after):
    """Retain change kinds; a rename includes a deletion with --no-renames."""
    fields = _nul_paths(_git_bytes(git, [
        "diff", "--name-status", "--no-renames", "-z", before, after,
    ]))
    return set(zip(fields[::2], fields[1::2], strict=True))


def disjoint_inputs(paths, own):
    """The explicit CI inputs and the measured work define neutrality."""
    return not set(paths) & set(own) and not any(
        path.startswith(".github/workflows/")
        or PurePosixPath(path).name in {"conftest.py", "pyproject.toml"}
        for path in paths)


def disjoint_changes(changes, own):
    """Only additions and modifications can leave an active train neutral."""
    return (all(status in {"A", "M"} for status, _ in changes)
            and disjoint_inputs({path for _, path in changes}, own))


def disjoint_move(git, move, own):
    """Every intervening commit must leave the measured inputs unchanged."""
    return (git.is_ancestor(move.head_sha, move.target_sha)
            and disjoint_changes(_moved_changes(git, move), own))


def train_in_flight(git):
    """Read live train refs at every effective push destination; failures propagate."""
    # Train names encode a base commit, not a target branch. Until the owner
    # publishes target identity, any train ref may belong to this base.
    destinations = git._call(["remote", "get-url", "--push", "--all", "origin"]).splitlines()
    return any(git._call([
        "ls-remote", "--heads", destination, "refs/heads/foragent/batch-merge-*",
    ]).strip() for destination in destinations)


def wait_for_merge_train(git, config):
    """Wait before publication changes the base, so timeout leaves no retained commit."""
    if not getattr(config, "merge_owner", False):
        return
    started = time.monotonic()
    while train_in_flight(git):
        elapsed = time.monotonic() - started
        if elapsed >= 40 * 60:
            raise GitError("timed out after 40 minutes waiting for merge train; "
                           "no publication commit created; retry publication")
        print(f"waiting for merge train ({int(elapsed // 60)} min)", file=sys.stderr, flush=True)
        time.sleep(min(30, 40 * 60 - elapsed))


def push_base(git, config, *, owner=None):
    if getattr(config, "merge_owner", False) and train_in_flight(git):
        from foragent.merge_owner import may_write_target

        if owner is None or not may_write_target(
                owner, config.base_branch, changed_inputs(git, f"origin/{config.base_branch}", "HEAD")):
            raise GitError("waiting for merge train")
    # A wait/read failure uses each writer's existing failed-push retry path.
    git.push_base(config.base_branch)
