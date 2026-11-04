"""Role memory reaches the base branch from the ordinary gate tick (#1699).

A role session leaves its memory edits in the checkout. Each tick commits the changed and
new files of every role's memory directory, ignored ones too, under the one main-write lock
(#1662), and only then talks to the remote: the local commit never waits on a fetch, so a
checkout cleanup cannot lose it. Delivery fetches, rebuilds the unpushed role-memory commits on
a moved remote base (the way a note flush does) and pushes the base branch. A failed delivery
keeps the local commit; the retry is derived from Git (local base ahead of origin by
role-memory commits only), so no state is stored. No file name is parsed from Git output: Git
receives the directories as arguments and answers with exit codes and commit IDs.
Lane memory stays with the dream: a lane that shares a role's directory does not dream.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from foragent.git import GitError
from foragent.roles import load_roles

if TYPE_CHECKING:
    from foragent.scheduler import TickContext, TickReport


def _directories(ctx: TickContext) -> dict[str, list[str]]:
    """Each role's memory directory (relative, no trailing ``/``) that exists, with its roles."""
    folders: dict[str, list[str]] = {}
    for name, entry in load_roles(ctx.config).items():
        folder = entry["memory"].rstrip("/")
        if (ctx.repo_root / folder).is_dir():
            folders.setdefault(folder, []).append(name)
    return dict(sorted(folders.items()))


def _differs(ctx: TickContext, *args: str) -> bool:
    """Whether ``git diff --quiet <args>`` finds a change: its exit code, no path is read back."""
    try:
        ctx.git._call(["diff", "--quiet", *args])
    except GitError as exc:
        if getattr(exc.__cause__, "returncode", None) == 1:
            return True
        raise
    return False


def _title(ctx: TickContext, folders: dict[str, list[str]], *revisions: str) -> str:
    """Name the roles whose directory ``git diff <revisions>`` changes."""
    roles = sorted({role for folder, names in folders.items()
                    if _differs(ctx, *revisions, "--", folder) for role in names})
    return f"Foragent role memory: {', '.join(roles)}"


def _owed(ctx: TickContext, base: str, folders: dict[str, list[str]]) -> bool:
    """Whether local base holds unpushed commits, none of which changes anything outside role memory.

    Git answers by pathspec and returns commit IDs only, so no file name is parsed here.
    """
    remote = f"refs/remotes/origin/{base}"
    try:
        ctx.git._call(["rev-parse", "--verify", "--quiet", remote])
    except GitError:
        return False
    if not ctx.git._call(["rev-list", f"{remote}..HEAD"]).split():
        return False
    outside = ctx.git._call(["log", "--format=%H", "--full-history", "-m", f"{remote}..HEAD", "--",
                             ":(top)", *(f":(top,exclude,literal){folder}" for folder in folders)])
    return not outside.split()


def _commit(ctx: TickContext, folders: dict[str, list[str]]) -> bool:
    """Commit the role memory changes locally; no remote is read before this.

    The directories themselves go to Git as separate arguments, so no file name inside them is
    ever read back and re-sent: any byte a name holds reaches the commit unchanged.
    """
    names = list(folders)
    ctx.git._call(["add", "-A", "-f", "--", *names])
    if not _differs(ctx, "--cached", "HEAD", "--", *names):
        return False
    ctx.git.commit_paths(names, _title(ctx, folders, "--cached", "HEAD"))
    return True


def _rebuild(ctx: TickContext, base: str, folders: dict[str, list[str]]) -> None:
    """Put the unpushed role-memory commits on origin's moved base, keeping staged and unstaged work."""
    remote = f"refs/remotes/origin/{base}"
    old = ctx.git._call(["rev-parse", "HEAD"]).strip()
    title = _title(ctx, folders, f"{remote}...{old}")
    tree = ctx.git._call(["merge-tree", "--write-tree", remote, old]).splitlines()[0]
    identity = []
    for key, fallback in (("user.name", "foragent"), ("user.email", "foragent@localhost")):
        value = ctx.git._call(["config", "--default", fallback, "--get", key]).strip()
        identity.extend(["-c", f"{key}={value or fallback}"])
    new = ctx.git._call([*identity, "commit-tree", tree, "-p", remote, "-m", title]).strip()
    # Two-tree read-tree keeps staged and unstaged changes on paths the rebuild leaves alone.
    ctx.git._call(["read-tree", "-m", "-u", old, new])
    ctx.git._call(["update-ref", f"refs/heads/{base}", new, old])


def _deliver(ctx: TickContext, base: str, folders: dict[str, list[str]]) -> str:
    ctx.git.fetch("origin", base)
    remote = f"refs/remotes/origin/{base}"
    if not ctx.git.is_ancestor(remote, "HEAD"):
        if not _owed(ctx, base, folders):
            return "not-owed"  # Other unpushed work shares the base; its own writer integrates it.
        _rebuild(ctx, base, folders)
    ctx.git.push_base(base)
    return "pushed"


def publish(ctx: TickContext, report: TickReport) -> None:
    folders = _directories(ctx)
    if not folders:
        return
    base = ctx.config.base_branch
    try:
        if ctx.git._call(["symbolic-ref", "--quiet", "--short", "HEAD"]).strip() != base:
            return  # The checkout is on another branch; its base is published when it returns.
        # Only emptiness is read: any entry, ignored files included, means work to commit.
        changed = bool(ctx.git._call(["status", "--porcelain", "-z", "--ignored", "--untracked-files=all",
                                      "--", *folders]))
        if not changed and not _owed(ctx, base, folders):
            return
        with ctx.git.publish_lock():
            committed = changed and _commit(ctx, folders)
            if not committed and not _owed(ctx, base, folders):
                return
            try:
                if _deliver(ctx, base, folders) == "not-owed":
                    report.actions.append(f"role memory committed on {base}; other unpushed commits "
                                          "share the base, so delivery waits for them")
                    return
            except GitError as exc:
                report.actions.append(f"role memory committed on {base}; delivery failed and is retried "
                                      f"next tick: {_one_line(exc)}")
                return
            report.actions.append(f"role memory published to {base}")
    except GitError as exc:
        report.actions.append(f"role memory not published: {_one_line(exc)}")


def _one_line(exc: Exception) -> str:
    return " ".join(str(exc).split())[:300]
