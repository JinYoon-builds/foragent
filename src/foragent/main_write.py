"""Admission for non-owner base pushes, under the writer's existing main-write lock."""

from foragent.git import GitError


def push_base(git, config):
    # Train names encode a base commit, not a target branch. Until the owner
    # publishes target identity, any train ref may belong to this base.
    if getattr(config, "merge_owner", False) and git._call([
        "ls-remote", "--heads", "origin", "refs/heads/foragent/batch-merge-*",
    ]).strip():
        raise GitError("waiting for merge train")
    # A wait/read failure uses each writer's existing failed-push retry path.
    git.push_base(config.base_branch)
