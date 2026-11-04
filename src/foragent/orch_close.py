"""Close superseded, never-merged work without changing the user's checkout.

The command owns lock removal; the shared main-write lock and an explicit remote
lease serialize its base update. Tracker owns PR/issue writes, orch_events the
audit copy. No progress record: each retry reads what remains at the origin.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory

from foragent.git import GitRepo, main_write_lock
from foragent.i18n import text
from foragent.orch_events import append_event
from foragent.scheduler_stop import unfinished_runs
from foragent.tracker import RecordingTracker, pr_issue_number


def _remove_locks(ctx, n: int, superseded_by: int, merged: set[int]) -> None:
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
    message = f"Remove superseded #{n} locked tests (superseded by #{superseded_by})"
    commit = ctx.git._call([*identity, "commit-tree", tree, "-p", tip, "-m", message]).strip()
    ctx.git._call(["push", f"--force-with-lease=refs/heads/{base}:{tip}",
                   "origin", f"{commit}:refs/heads/{base}"])


def close(ctx, n: int, superseded_by: int, why: str) -> str:
    from foragent.orch import OrchError

    language = ctx.config.language
    if n <= 0 or superseded_by <= 0 or not why.strip():
        raise OrchError(text("orch_close_arguments", language))
    with ctx.store.hold_tick_lock() as acquired:
        if not acquired:
            raise OrchError(text("orch_close_busy", language, n=n))
        with ctx.store.merge_lock(), main_write_lock(ctx.git):
            # Read directly: refusal must not even ingest a new observation.
            issue = ctx.tracker.get_issue(n)
            if "running" in issue.labels or any(run.number == n for run in unfinished_runs(ctx.store)):
                raise OrchError(text("orch_close_running", language, n=n))
            merged = {run.number for run in ctx.store.read_history() if run.outcome == "merged"}
            merged.update(number for number, _ in ctx.store.merge_times())
            merged.update(number for pr in ctx.tracker.list_prs("merged", "foragent/")
                          if (number := pr_issue_number(pr.head)) is not None)
            if n in merged:
                raise OrchError(text("orch_close_merged", language, n=n))
            if issue.is_pull_request:
                raise OrchError(text("orch_close_issue_required", language, n=n))
            summary = text("orch_close_done", language, n=n, by=superseded_by, why=why.strip())
            pr = ctx.tracker.find_pr(f"foragent/issue-{n}")
            comment = f"<!-- foragent:orch kind=closed -->\n{summary}"
            published = any(item.body == comment for item in ctx.tracker.list_comments(n))
            _remove_locks(ctx, n, superseded_by, merged)
            tracker = RecordingTracker(ctx.tracker, ctx)
            if pr is not None:
                tracker.close_pr(pr, delete_branch=True)
            if issue.state == "closed" and published:
                return summary
            if not published:
                tracker.comment(n, comment)
            if issue.state != "closed":
                tracker.close_issue(n)
            append_event(ctx.store, ctx.now(), n, "closed", summary)
            return summary
