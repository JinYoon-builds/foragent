"""Close superseded, never-merged work without changing the user's checkout.

With the merge owner enabled, lock removal is a durable owner request. Otherwise
the command owns lock removal; the shared main-write lock and an explicit remote
lease serialize its base update. Tracker owns PR/issue writes, orch_events the
audit copy. No progress record: each retry reads what remains at the origin.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
import subprocess
from collections.abc import Iterable
from tempfile import TemporaryDirectory

from foragent.git import GitRepo, main_write_lock
from foragent.i18n import text
from foragent.board_parents import replacements
from foragent.orch_events import append_event
from foragent.scheduler_stop import unfinished_runs
from foragent.tracker import RecordingTracker, TrackerError, pr_issue_number


def _remove_locks(ctx, n: int, superseded_by: int | tuple[int, ...], merged: set[int]) -> None:
    superseded_by = (superseded_by,) if isinstance(superseded_by, int) else superseded_by
    by = ", ".join(f"#{number}" for number in superseded_by)
    approval = ctx.store.load_approval(n)
    if approval is None or not approval.acceptance_blobs:
        return
    paths = set(approval.acceptance_blobs)
    for number in merged:
        other = ctx.store.load_approval(number)
        if other is not None:
            paths.difference_update(other.acceptance_blobs)
    if not paths:
        return
    base = ctx.config.base_branch
    if ctx.config.merge_owner:
        from foragent import main_write_requests

        run = ctx.store.get_run(n)
        requester = f"lane {run.lane} issue #{n}" if run else f"issue #{n}"
        main_write_requests.submit(
            ctx.store, base, f"Remove superseded #{n} locked tests (superseded by {by})", requester,
            {"kind": "close-locks", "n": n, "paths": {name: approval.acceptance_blobs[name] for name in paths}},
            identity=f"close-locks:{n}:{'-'.join(map(str, superseded_by))}")
        return
    ctx.git.fetch("origin", base)
    tip = ctx.git._call(["rev-parse", f"refs/remotes/origin/{base}"]).strip()
    files = set(ctx.git._call(["ls-tree", "-rz", "--name-only", tip]).split("\0"))
    paths.intersection_update(files)
    if not paths:
        return
    # A private index avoids both staging and checking out any user's files.
    with TemporaryDirectory(prefix="foragent-close-") as temporary:
        environment = {**os.environ, "GIT_INDEX_FILE": str(Path(temporary) / "index")}

        def run(args):
            return subprocess.run(["git", *args], cwd=ctx.git.root, env=environment,
                                  check=True, capture_output=True, text=True, timeout=120).stdout

        git = GitRepo(ctx.git.root, run=run)
        git._call(["read-tree", tip])
        git._call(["update-index", "--force-remove", "--", *sorted(paths)])
        tree = git._call(["write-tree"]).strip()
    identity = []
    for key, fallback in (("user.name", "foragent"), ("user.email", "foragent@localhost")):
        value = ctx.git._call(["config", "--default", fallback, "--get", key]).strip()
        identity.extend(["-c", f"{key}={value or fallback}"])
    message = f"Remove superseded #{n} locked tests (superseded by {by})"
    commit = ctx.git._call([*identity, "commit-tree", tree, "-p", tip, "-m", message]).strip()
    ctx.git._call(["push", f"--force-with-lease=refs/heads/{base}:{tip}",
                   "origin", f"{commit}:refs/heads/{base}"])


def close_with_pr(tracker, n: int, comment: str | None = None, *, reason: str | None = None,
                  issue_open: bool | None = None) -> None:
    """The one close of an issue: its open Foragent PR and branch go first, then the issue.

    `fa orch close` and an order's cleanup close both call this. The PR closes before the
    issue, so a failure between the two writes leaves the issue open for the next run to
    finish; `find_pr` returns open PRs only, so a repeated close deletes nothing more.
    `comment` goes with the close, or alone when the issue is already closed (gh returns
    early on a closed issue before it posts `--comment`).
    """
    pr = tracker.find_pr(f"foragent/issue-{n}")
    if pr is not None:
        tracker.close_pr(pr, delete_branch=True)
    if issue_open is None:
        issue_open = tracker.get_issue(n).state != "closed"
    if issue_open:
        tracker.close_issue(n, comment, reason=reason)
    elif comment is not None:
        tracker.comment(n, comment)


def _merged_issues(ctx):
    owner_facts = ctx.store.owner_merge_facts()
    merged = {run.number for run in ctx.store.read_history() if ctx.store.run_merged(run, owner_facts=owner_facts)}
    merged.update(number for number, _ in ctx.store.merge_times())
    merged.update(number for pr in ctx.tracker.list_prs("merged", "foragent/")
                  if (number := _merged_head_issue(pr.head)) is not None)
    return merged


def _merged_head_issue(head: str) -> int | None:
    # Retired batch PRs stay merged on GitHub; their history still counts as landed work.
    if (number := pr_issue_number(head)) is not None:
        return number
    match = re.fullmatch(r"foragent/batch-(\d+)", head)
    return int(match[1]) if match else None


def close(ctx, n: int, superseded_by: int | Iterable[int], why: str) -> str:
    """Close #n as replaced by one issue or several (a split), then start a pass at once.

    The journal records every replacement; the closed issue's files and blockers are free.
    """
    from foragent import orch

    summary = _close(ctx, n, superseded_by, why)
    orch.kick_tick(ctx.repo_root)
    return summary


def _close(ctx, n: int, superseded_by: int | Iterable[int], why: str) -> str:
    from foragent.orch import OrchError

    language = ctx.config.language
    by = tuple(dict.fromkeys([superseded_by] if isinstance(superseded_by, int) else superseded_by))
    if n <= 0 or not by or min(by) <= 0 or n in by or not why.strip():
        raise OrchError(text("orch_close_arguments", language))
    with ctx.store.hold_tick_lock() as acquired:
        if not acquired:
            raise OrchError(text("orch_close_busy", language, n=n))
        with ctx.store.merge_lock(), main_write_lock(ctx.git):
            # Read directly: refusal must not even ingest a new observation.
            try:
                issue = ctx.tracker.get_issue(n)
            except TrackerError as exc:
                if exc.is_not_found:
                    raise OrchError(text("orch_close_missing", language, n=n)) from exc
                raise
            if "running" in issue.labels or any(run.number == n for run in unfinished_runs(ctx.store)):
                raise OrchError(text("orch_close_running", language, n=n))
            merged = _merged_issues(ctx)
            if n in merged:
                raise OrchError(text("orch_close_merged", language, n=n))
            if issue.is_pull_request:
                raise OrchError(text("orch_close_issue_required", language, n=n))
            summary = text("orch_close_done", language, n=n, by=", ".join(f"#{number}" for number in by), why=why.strip())
            comment = f"<!-- foragent:orch kind=closed -->\n{summary}"
            published = any(item.body == comment for item in ctx.tracker.list_comments(n))
            _remove_locks(ctx, n, by, merged)
            tracker = RecordingTracker(ctx.tracker, ctx)
            # Superseded work never shipped: GitHub's default reason (completed) would say it did.
            close_with_pr(tracker, n, None if published else comment, reason="not_planned",
                          issue_open=issue.state != "closed")
            if issue.state == "closed" and published and replacements(ctx.store, n) == by:
                return summary
            # The order's closing comment and the issue view read the replacements from this event.
            append_event(ctx.store, ctx.now(), n, "closed", summary, superseded_by=by[0], replaced_by=list(by))
            return summary
