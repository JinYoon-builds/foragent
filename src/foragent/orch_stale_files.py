"""Report approved Files entries that no longer match anything on origin/<base>.

Facts only: the gate records one ``stale-files`` orch event per issue and base
SHA so the orchestrator can remap Files; nothing is held, unapproved or reordered.
"""

from datetime import UTC, datetime
from glob import has_magic
import json
from pathlib import PurePosixPath

from foragent.git import GitError
from foragent.i18n import text
from foragent.issue import parse_body
from foragent.orch_events import append_event
from foragent.policy import _matches
from foragent.tracker import local_issue_models

KIND = "stale-files"
STATE_FILE = "stale-files.json"


def _matching(entry: str, files: list[str]) -> bool:
    files = [path for path in files if path]  # Callers may pass raw listings.
    if has_magic(entry):
        return any(_matches(path, entry) for path in files)
    entry = entry.rstrip("/")
    return any(path == entry or path.startswith(entry + "/") for path in files)


def _glob_prefix(entry: str) -> str:
    """The fixed directory part of a glob, before its first component with magic."""
    fixed = []
    for part in PurePosixPath(entry).parts:
        if has_magic(part):
            break
        fixed.append(part)
    return "/".join(fixed)


def _directory_exists(prefix: str, files: list[str]) -> bool:
    return prefix == "" or any(path.startswith(prefix + "/") for path in files)


def stale_entries(entries: list[str], then: list[str], now: list[str]) -> list[str]:
    """Entries that matched a file at approval and match nothing now.

    A glob is also stale when its fixed directory prefix existed at approval and
    is gone now. A plain path that never existed is a planned new file.
    """
    stale = []
    for entry in entries:
        if _matching(entry, then) and not _matching(entry, now):
            stale.append(entry)
        elif has_magic(entry):
            prefix = _glob_prefix(entry)
            if prefix and _directory_exists(prefix, then) and not _directory_exists(prefix, now):
                stale.append(entry)
    return stale


def _read(path) -> dict[str, list[str]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {key: [sha for sha in shas if isinstance(sha, str)]
            for key, shas in value.items() if isinstance(shas, list)}


def approval_base(ctx, base_ref: str, approved_at: str) -> str | None:
    """origin/<base> as of ``approved_at``: its last first-parent commit at or before then.

    Only the base branch's own (first-parent) line counts; commits of a topic
    branch merged later keep their earlier dates but were not on the base then.
    """
    approved = datetime.fromisoformat(approved_at).astimezone(UTC)
    return ctx.git._call([
        "rev-list", "-1", "--first-parent", f"--until={int(approved.timestamp())}", base_ref,
    ]).strip() or None


def _record(ctx, path, number: int, sha: str) -> None:
    """Add one reported (issue, base SHA); history is never dropped, so no pair repeats."""
    with ctx.store._state_lock():
        seen = _read(path)
        shas = seen.setdefault(str(number), [])
        if sha not in shas:
            shas.append(sha)
        ctx.store._write_json(path, seen)


def report(ctx) -> None:
    """Scan every open issue that has an approval snapshot; never raises GitError."""
    base_ref = f"origin/{ctx.config.base_branch}"
    path = ctx.store.root / STATE_FILE
    candidates = []  # Issues with approved Files; Git is read only when there are some.
    for issue in local_issue_models(ctx.store):
        approval = ctx.store.load_approval(issue.number) if issue.state == "open" else None
        if approval is not None and (entries := parse_body(approval.body).files):
            candidates.append((issue.number, approval.approved_at, entries))
    if not candidates:
        return
    try:
        head = ctx.git._call(["rev-parse", base_ref]).strip()
    except GitError:
        return
    trees: dict[str, list[str]] = {}

    def tree(sha: str) -> list[str]:
        if sha not in trees:
            # An empty tree lists nothing; "" is not a file name (it would match "**").
            listing = ctx.git._call(["ls-tree", "-r", "--name-only", "-z", sha])
            trees[sha] = [name for name in listing.split("\0") if name]
        return trees[sha]

    with ctx.store._state_lock():
        seen = _read(path)
    for number, approved_at, entries in candidates:
        if head in seen.get(str(number), []):
            continue
        try:
            then_sha = approval_base(ctx, base_ref, approved_at)
            if then_sha is None:
                continue
            stale = stale_entries(entries, tree(then_sha), tree(head))
        except (GitError, ValueError):
            continue
        if not stale:
            continue
        values = {"number": number, "base": base_ref, "sha": head[:12], "entries": ", ".join(stale)}
        say = text("stale_files", ctx.config.language, **values)
        append_event(ctx.store, ctx.now(), number, KIND, text("stale_files", "en", **values),
                     say=say if ctx.config.language != "en" else None)
        _record(ctx, path, number, head)
