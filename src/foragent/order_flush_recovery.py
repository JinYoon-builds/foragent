"""Recover a single publication intent from Git facts, without undoing commits."""

from foragent import order_flush_journal as journal
from foragent.i18n import text
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


def _find(git, remote, batch):
    records = git._call(["log", "--format=%H%x00%s", f"{remote}..HEAD"]).splitlines()
    return next((line.split("\0", 1)[0] for line in records
                 if line.partition("\0")[2] == title(batch)), None)


def _install(git, config, old, new):
    # Two-tree read-tree keeps staged AND unstaged changes on unchanged paths.
    # It also tolerates re-entry after the tree was installed but the ref wasn't moved.
    git._call(["read-tree", "-m", "-u", old, new])
    git._call(["update-ref", f"refs/heads/{config.base_branch}", new, old])


def committed(root, config, git, store, batch, records):
    """Return the recognized SHA, or restore the uncommitted intent for retry."""
    remote = f"origin/{config.base_branch}"
    git.fetch("origin", config.base_branch)
    history = [row for row in records if row["batch"] == batch["batch"]]
    saved = [row for row in history if row["kind"] in {"committed", "rebased"}]
    sha = saved[-1]["sha"] if saved else None
    # A non-ancestor is handled before any moved-base rebuild. The ref may
    # already name a rebuilt commit whose rebased record was interrupted.
    if sha is None or not git.is_ancestor(sha, "HEAD"):
        found = _find(git, remote, batch)
        if found:
            journal.append(store, "rebased" if sha else "committed", batch["batch"], sha=found, old=sha)
            return found
        restore(root, git, batch["paths"])
        return None
    rebuilding = [row for row in history if row["kind"] == "rebuilding"]
    if rebuilding and history.index(rebuilding[-1]) > history.index(saved[-1]):
        intent = rebuilding[-1]
        if git.head_sha() == intent["old"]:
            _install(git, config, intent["old"], intent["sha"])
            journal.append(store, "rebased", batch["batch"], old=intent["old"], sha=intent["sha"])
            sha = intent["sha"]
    return sha


def push(config, git, store, batch, sha, *, recovering):
    remote = f"origin/{config.base_branch}"
    if recovering:
        if git.is_ancestor(sha, remote):
            journal.append(store, "pushed", batch["batch"], sha=sha)
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
            journal.append(store, "rebuilding", batch["batch"], old=sha, sha=rebuilt)
            _install(git, config, sha, rebuilt)
            journal.append(store, "rebased", batch["batch"], old=sha, sha=rebuilt)
            sha = rebuilt
    git.push_base(config.base_branch)
    journal.append(store, "pushed", batch["batch"], sha=sha)
    return sha
