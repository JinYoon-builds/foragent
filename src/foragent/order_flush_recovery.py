"""Recover a single publication intent from Git facts, without undoing commits."""

from foragent.i18n import text
from foragent.main_write import push_base
from foragent.order import PlanError
from foragent.order_files import check_write
from foragent.paths import repo_path


def title(batch):
    return f"Foragent batch {' '.join(batch['boundary'].split())} {batch['batch']}"


def restore(root, git, paths):
    """Release exactly the intent's paths, in both the index and worktree."""
    for name in paths:
        check_write(root, name, ".")
        if git.blob_sha("HEAD", name) is not None:
            git._call(["--literal-pathspecs", "restore", "--source=HEAD", "--staged", "--worktree", "--", name])
        else:
            git._call(["--literal-pathspecs", "rm", "--cached", "-f", "--ignore-unmatch", "--", name])
            repo_path(root, name).unlink(missing_ok=True)


def _find(git, rev, batch):
    """The batch commit in ``rev``'s whole history: its title carries the batch's unique id."""
    records = git._call(["log", "--format=%H%x00%s", "--fixed-strings", f"--grep={title(batch)}", rev]).splitlines()
    return next((line.split("\0", 1)[0] for line in records
                 if line.partition("\0")[2] == title(batch)), None)


def _install(git, config, old, new):
    # Two-tree read-tree keeps staged AND unstaged changes on unchanged paths.
    # It also tolerates re-entry after the tree was installed but the ref wasn't moved.
    git._call(["read-tree", "-m", "-u", old, new])
    git._call(["update-ref", f"refs/heads/{config.base_branch}", new, old])


def _land(root, config, git, batch, local, published):
    """Bring HEAD to a batch commit the remote already has, repeating nothing."""
    if git.is_ancestor(published, "HEAD"):
        return
    if local is None:
        restore(root, git, batch["paths"])
        git.update_base(config.base_branch)
    elif git.head_sha() == local:
        _install(git, config, local, published)
    else:
        raise PlanError(text("order_flush_descendants", config.language))


def _rebuild_ref(batch):
    return f"refs/foragent/flush/{batch['batch']}"


def committed(root, config, git, batch):
    """Recover from Git history and any unfinished tree/ref installation."""
    remote = f"origin/{config.base_branch}"
    git.fetch("origin", config.base_branch)
    found = _find(git, remote, batch)
    local = _find(git, "HEAD", batch)
    ref = _rebuild_ref(batch)
    intent = git._call(["for-each-ref", "--format=%(objectname)", ref]).strip() or None
    if found:
        if intent and local and local != intent:
            # The tree may have moved before the branch ref did. Restore it first.
            git._call(["read-tree", "-m", "-u", intent, local])
        _land(root, config, git, batch, local, found)
        if intent:
            git._call(["update-ref", "-d", ref, intent])
        return found
    if local is None:
        restore(root, git, batch["paths"])
        return None
    if intent:
        if local != intent:
            if git.head_sha() != local:
                raise PlanError(text("order_flush_descendants", config.language))
            _install(git, config, local, intent)
        git._call(["update-ref", "-d", ref, intent])
        return intent
    return local


def push(config, git, batch, sha, *, recovering):
    remote = f"origin/{config.base_branch}"
    if recovering:
        if git.is_ancestor(sha, remote):
            return sha
        if not git.is_ancestor(remote, "HEAD"):
            if git.head_sha() != sha:
                raise PlanError(text("order_flush_descendants", config.language))
            tree = git._call(["merge-tree", "--write-tree", remote, sha]).splitlines()[0]
            identity = []
            for key, fallback in (("user.name", "foragent"), ("user.email", "foragent@localhost")):
                value = git._call(["config", "--default", fallback, "--get", key]).strip()
                identity.extend(["-c", f"{key}={value or fallback}"])
            rebuilt = git._call([*identity, "commit-tree", tree, "-p", remote, "-m", title(batch)]).strip()
            ref = _rebuild_ref(batch)
            git._call(["update-ref", ref, rebuilt])
            _install(git, config, sha, rebuilt)
            git._call(["update-ref", "-d", ref, rebuilt])
            sha = rebuilt
    push_base(git, config)
    return sha
