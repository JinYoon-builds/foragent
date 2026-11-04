"""Merge-train admission for base writers.

Publication waits leave the shared main-write lock available to the owner.
The owner can still create a train between the ref lookup and the push.
"""

import sys
import time

from foragent.git import GitError


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


def push_base(git, config):
    if getattr(config, "merge_owner", False) and train_in_flight(git):
        raise GitError("waiting for merge train")
    # A wait/read failure uses each writer's existing failed-push retry path.
    git.push_base(config.base_branch)
