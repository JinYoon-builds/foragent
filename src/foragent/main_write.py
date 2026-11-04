"""Admission for retryable base writers, under their existing main-write lock.

The owner can still create a train between the ref lookup and the push.
"""

from foragent.git import GitError


def push_base(git, config):
    # Train names encode a base commit, not a target branch. Until the owner
    # publishes target identity, any train ref may belong to this base.
    if getattr(config, "merge_owner", False):
        destinations = git._call(["remote", "get-url", "--push", "--all", "origin"]).splitlines()
        for destination in destinations:
            if git._call([
                "ls-remote", "--heads", destination, "refs/heads/foragent/batch-merge-*",
            ]).strip():
                raise GitError("waiting for merge train")
    # A wait/read failure uses each writer's existing failed-push retry path.
    git.push_base(config.base_branch)
