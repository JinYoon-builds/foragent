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

The base branch moves for role memory at most once per ``WINDOW`` (r456): every main move makes
the merge owner refresh waiting PRs. Within the window after a successful push, a tick keeps the
roles' edits as a snapshot commit under ``PENDING``, a ref outside ``refs/heads``, so the local base
never holds a commit the remote lacks, and a checkout cleanup or a gate restart cannot lose them.
Each held tick merges the old snapshot with the current folders (merge base: the snapshot's
parent), so later lessons join earlier ones. The first tick after the window restores the snapshot
into the folders and publishes it in one commit. ``role-memory.json`` in the state store holds the
time of the last successful push; only this module writes it, and a missing record means "due".
"""

from __future__ import annotations

from datetime import datetime, timedelta
import json
import os
from typing import TYPE_CHECKING

from foragent.git import GitError
from foragent.roles import load_roles

if TYPE_CHECKING:
    from foragent.scheduler import TickContext, TickReport

WINDOW = timedelta(minutes=30)
PENDING = "refs/foragent/role-memory-pending"
RECORD = "role-memory.json"


def _pushed_at(ctx: TickContext) -> datetime | None:
    """The time of the last successful role-memory push; None when unknown (then a push is due)."""
    try:
        return datetime.fromisoformat(json.loads((ctx.store.root / RECORD).read_text())["pushed_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _record_push(ctx: TickContext) -> None:
    path = ctx.store.root / RECORD
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{RECORD}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps({"pushed_at": ctx.now().isoformat()}) + "\n")
    os.replace(temporary, path)


def _held_until(ctx: TickContext) -> datetime | None:
    """When the window that holds role memory closes, or None when a push is due now."""
    pushed = _pushed_at(ctx)
    if pushed is None:
        return None
    until = pushed + WINDOW
    try:
        return until if ctx.now() < until else None
    except TypeError:  # A naive record against an aware clock: treat as due rather than hold forever.
        return None


def _identity(ctx: TickContext) -> list[str]:
    identity = []
    for key, fallback in (("user.name", "foragent"), ("user.email", "foragent@localhost")):
        value = ctx.git._call(["config", "--default", fallback, "--get", key]).strip()
        identity.extend(["-c", f"{key}={value or fallback}"])
    return identity


def _pending(ctx: TickContext) -> str | None:
    try:
        return ctx.git._call(["rev-parse", "--verify", "--quiet", f"{PENDING}^{{commit}}"]).strip() or None
    except GitError:
        return None


def _roles(ctx: TickContext) -> dict[str, list[str]]:
    """Every configured role memory directory (relative, no trailing ``/``) with its roles."""
    folders: dict[str, list[str]] = {}
    for name, entry in load_roles(ctx.config).items():
        folders.setdefault(entry["memory"].rstrip("/"), []).append(name)
    return dict(sorted(folders.items()))


def _present(ctx: TickContext, roles: dict[str, list[str]]) -> dict[str, list[str]]:
    """The role directories that hold a file in the checkout; only these are staged from it.

    An empty or missing directory is never a pathspec: Git cannot match it, and a held lesson for it
    stays in the pending commit until it is restored from there.
    """
    return {folder: names for folder, names in roles.items()
            if any(not path.is_dir() for path in (ctx.repo_root / folder).rglob("*"))}


def _in(ctx: TickContext, roles: dict[str, list[str]], commit: str) -> list[str]:
    """The role directories a commit holds, whether or not the checkout still has them.

    A held lesson lives in the commit, so a directory deleted from the checkout is still restored
    and published; a pathspec Git cannot match is never passed.
    """
    held = []
    for folder in roles:
        try:
            ctx.git._call(["rev-parse", "--verify", "--quiet", f"{commit}:{folder}"])
        except GitError:
            continue
        held.append(folder)
    return held


def _restore(ctx: TickContext, roles: dict[str, list[str]], commit: str, *where: str) -> None:
    folders = _in(ctx, roles, commit)
    if folders:
        ctx.git._call(["restore", f"--source={commit}", *where, "--", *folders])


def _snapshot(ctx: TickContext, present: dict[str, list[str]]) -> str:
    """A commit on HEAD whose tree holds the role folders as they are now, ignored files included.

    The index is borrowed under the publish lock and the folders' entries are put back to HEAD,
    so nothing is left staged; no file name is read back from Git. A directory missing from the
    checkout keeps its index entries, so the snapshot does not record it as deleted.
    """
    names = list(present)
    if names:
        ctx.git._call(["add", "-A", "-f", "--", *names])
    try:
        tree = ctx.git._call(["write-tree"]).strip()
    finally:
        if names:
            ctx.git._call(["restore", "--staged", "--", *names])
    return ctx.git._call([*_identity(ctx), "commit-tree", tree, "-p", "HEAD",
                          "-m", "Foragent role memory snapshot"]).strip()


def _merge(ctx: TickContext, pending: str, snapshot: str) -> tuple[str, bool]:
    """Join the held snapshot with the current one; (tree, whether a held version was superseded).

    The checkout is synced to the held snapshot on every held tick, so a file it changed since is
    newer: where the two cannot be joined the checkout's text wins (``-X theirs``), and appended
    texts are joined by Git's built-in union driver (an attribute for this merge only). When the
    plain merge could not join them (binary content, for example), the held version is reported
    as superseded, and the caller keeps it in history instead of dropping it.
    """
    parent = ctx.git._call(["rev-parse", f"{pending}^"]).strip()
    attributes = ctx.git.common_dir() / "foragent" / "role-memory.attributes"
    if not attributes.is_file():
        attributes.parent.mkdir(parents=True, exist_ok=True)
        attributes.write_text("* merge=union\n")

    def merge(attributes_file: str, *options: str) -> tuple[str, bool]:
        try:
            output = ctx.git._call(["-c", f"core.attributesFile={attributes_file}", "merge-tree", "--write-tree",
                                    *options, "-X", "no-renames", f"--merge-base={parent}", pending, snapshot])
        except GitError as exc:
            cause = exc.__cause__
            if getattr(cause, "returncode", None) != 1 or not getattr(cause, "stdout", ""):
                raise
            # Still conflicted (a file deleted on one side): Git's tree keeps the changed file.
            return cause.stdout.splitlines()[0].strip(), False
        return output.splitlines()[0].strip(), True

    tree, joined = merge(str(attributes))
    if joined:
        return tree, False
    # The union driver would keep the held side of a binary file, so this pass runs without it.
    return merge(os.devnull, "-X", "theirs")[0], True


def _kept(ctx: TickContext, commit: str) -> str | None:
    """The superseded held snapshot a pending commit keeps for history (its second parent)."""
    try:
        return ctx.git._call(["rev-parse", "--verify", "--quiet", f"{commit}^2"]).strip() or None
    except GitError:
        return None


def _gather(ctx: TickContext, present: dict[str, list[str]], changed: bool,
            pending: str | None) -> tuple[str | None, str | None]:
    """Fold the folders' current state into the pending snapshot; (commit holding every lesson, snapshot).

    With a pending snapshot this always merges, so a cleaned checkout or a moved HEAD (a merged
    dream) loses neither the held lessons nor the newer base. A held version the merge superseded
    stays reachable as the new commit's second parent and is published before it.
    """
    if not changed and pending is None:
        return None, None
    snapshot = _snapshot(ctx, present)
    if pending is None:
        return snapshot, snapshot
    tree, superseded = _merge(ctx, pending, snapshot)
    keep = pending if superseded else _kept(ctx, pending)
    merged = ctx.git._call([*_identity(ctx), "commit-tree", tree, "-p", "HEAD", *(["-p", keep] if keep else []),
                            "-m", "Foragent role memory snapshot"]).strip()
    return merged, snapshot


def _hold(ctx: TickContext, roles: dict[str, list[str]], pending: str | None, commit: str,
          snapshot: str, until: datetime, report: TickReport) -> None:
    names = list(roles)
    new = (pending is None and _differs(ctx, "HEAD", f"{commit}^{{tree}}", "--", *names)) or (
        pending is not None and (_kept(ctx, commit) != _kept(ctx, pending)
                                 or _differs(ctx, f"{pending}^{{tree}}", f"{commit}^{{tree}}", "--", *names)))
    if new:
        # Durable first: the checkout is touched only once the commit holding it is on the ref.
        ctx.git._call(["update-ref", PENDING, commit])
        report.actions.append(f"role memory held until {until.isoformat()} (one push per "
                              f"{int(WINDOW.total_seconds() // 60)} minutes)")
    if (new or pending is not None) and _differs(ctx, f"{snapshot}^{{tree}}", f"{commit}^{{tree}}", "--", *names):
        # A cleanup dropped held lessons from the checkout: put them back so later writes build on
        # them. The merged commit holds everything the checkout had, so nothing is overwritten.
        _restore(ctx, roles, commit, "--worktree")


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


def _commit(ctx: TickContext, folders: dict[str, list[str]], roles: dict[str, list[str]]) -> bool:
    """Commit the role memory changes locally; no remote is read before this.

    The directories themselves go to Git as separate arguments, so no file name inside them is
    ever read back and re-sent: any byte a name holds reaches the commit unchanged.
    """
    names = list(folders)
    if not names:
        return False
    ctx.git._call(["add", "-A", "-f", "--", *names])
    if not _differs(ctx, "--cached", "HEAD", "--", *names):
        return False
    ctx.git.commit_paths(names, _title(ctx, roles, "--cached", "HEAD"))
    return True


def _rebuild(ctx: TickContext, base: str, folders: dict[str, list[str]]) -> None:
    """Replay the unpushed role-memory commits on origin's moved base, one by one, keeping staged work.

    Each commit keeps its own message and content, so a superseded held version published before
    the newest one stays in history.
    """
    remote = f"refs/remotes/origin/{base}"
    old = ctx.git._call(["rev-parse", "HEAD"]).strip()
    new = ctx.git._call(["rev-parse", remote]).strip()
    for commit in ctx.git._call(["rev-list", "--reverse", "--first-parent", f"{remote}..{old}"]).split():
        tree = ctx.git._call(["merge-tree", "--write-tree", f"--merge-base={commit}^", new, commit]).splitlines()[0]
        message = ctx.git._call(["log", "-1", "--format=%B", commit])
        new = ctx.git._call([*_identity(ctx), "commit-tree", tree, "-p", new, "-m", message.strip()]).strip()
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


def _stage(ctx: TickContext, roles: dict[str, list[str]], pending: str | None, commit: str | None) -> bool:
    """Put the held lessons back into the folders and commit locally; whether anything was committed.

    Superseded held versions are committed first, oldest first, so they reach history in the same
    delivery. The ref holds every version before the checkout is touched, and a failure part way
    puts the checkout back to the newest version, so a later tick never mistakes an older one for
    the latest write.
    """
    if pending is None:
        return _commit(ctx, _present(ctx, roles), roles)
    ctx.git._call(["update-ref", PENDING, commit])
    chain = [commit]
    while (older := _kept(ctx, chain[-1])) is not None:
        chain.append(older)
    committed = False
    try:
        for version in reversed(chain):
            _restore(ctx, roles, version, "--staged", "--worktree")
            committed = _commit(ctx, _present(ctx, roles), roles) or committed
    except GitError:
        _restore(ctx, roles, commit, "--worktree")
        raise
    return committed


def publish(ctx: TickContext, report: TickReport) -> None:
    roles = _roles(ctx)
    if not roles:
        return
    base = ctx.config.base_branch
    try:
        if ctx.git._call(["symbolic-ref", "--quiet", "--short", "HEAD"]).strip() != base:
            return  # The checkout is on another branch; its base is published when it returns.
        present = _present(ctx, roles)
        if not present and _pending(ctx) is None:
            return
        # Only emptiness is read: any entry, ignored files included, means work to commit.
        changed = bool(present) and bool(ctx.git._call(
            ["status", "--porcelain", "-z", "--ignored", "--untracked-files=all", "--", *present]))
        if not changed and _pending(ctx) is None and not _owed(ctx, base, roles):
            return
        with ctx.git.publish_lock():
            owed = _owed(ctx, base, roles)
            until = None if owed else _held_until(ctx)  # A failed delivery retries at once.
            pending = _pending(ctx)
            commit, snapshot = _gather(ctx, present, changed, pending)
            if until is not None and commit is not None and snapshot is not None:
                _hold(ctx, roles, pending, commit, snapshot, until, report)
                return
            committed = (changed or pending is not None) and _stage(ctx, roles, pending, commit)
            if pending is not None:
                # The lessons are in local commits now (or already on the base); the snapshot is done.
                ctx.git._call(["update-ref", "-d", PENDING, commit])
            if not committed and not _owed(ctx, base, roles):
                return
            try:
                if _deliver(ctx, base, roles) == "not-owed":
                    report.actions.append(f"role memory committed on {base}; other unpushed commits "
                                          "share the base, so delivery waits for them")
                    return
            except GitError as exc:
                report.actions.append(f"role memory committed on {base}; delivery failed and is retried "
                                      f"next tick: {_one_line(exc)}")
                return
            _record_push(ctx)
            report.actions.append(f"role memory published to {base}")
    except GitError as exc:
        report.actions.append(f"role memory not published: {_one_line(exc)}")


def _one_line(exc: Exception) -> str:
    return " ".join(str(exc).split())[:300]
