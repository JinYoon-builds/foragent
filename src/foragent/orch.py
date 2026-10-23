"""Orchestrator actions, with GitHub evidence and a local event journal."""

from collections import deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import math
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import time

from foragent.git import GitError, PublicationError
from foragent.kick import kick_tick
from foragent.labels import APPROVED, BLOCKED, NEEDS_HUMAN, READY, RUNNING, issue_lane
from foragent.order import detect_test_commands
from foragent.orch_events import append_event
from foragent.orch_reinstall import after_merge
from foragent.processes import run_logged
from foragent.runner import RunContext, review_only
from foragent.tracker import Comment, Issue, PullRequest, TrackerError, pr_issue_number


class OrchError(ValueError):
    """An orchestrator action does not apply to the current issue or PR."""


def _watch_snapshot(ctx: RunContext) -> dict:
    rows = ctx.store.local_issues()
    issues = [row for row in rows if row["state"] == "open"]
    return {
        "blocked": sorted(row["number"] for row in issues if BLOCKED in row["labels"]),
        "needs_human": sorted(row["number"] for row in issues if NEEDS_HUMAN in row["labels"]),
        "merged": sorted(row["number"] for row in rows if row["merged"]),
        "queue_empty": not any(set(row["labels"]) & {APPROVED, READY, RUNNING} for row in issues)
                       and not ctx.store.active_runs(),
    }


def watch(ctx: RunContext, *, interval: float = 5, follow: bool = False) -> Iterator[dict]:
    """Yield an initial snapshot, then only changes to the watched sets/state.

    The persisted snapshot lets a Monitor until-loop invoke this repeatedly
    without receiving the same event. Titles, timestamps and logs are excluded.
    """
    if not math.isfinite(interval) or interval <= 0:
        raise OrchError("interval must be a finite positive number")
    path = ctx.store.root / "orch-watch.json"
    while True:
        snapshot = _watch_snapshot(ctx)
        with ctx.store._state_lock():
            try:
                previous = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                previous = None
            changed = snapshot != previous
            if changed:
                ctx.store._write_json(path, snapshot)
        if changed:
            yield snapshot
            if not follow:
                return
        time.sleep(interval)


def _issue(ctx: RunContext, n: int) -> Issue:
    issue = ctx.tracker.get_issue(n)
    if issue.is_pull_request or issue.state != "open":
        raise OrchError(f"#{n} must be an open issue")
    run = ctx.store.get_run(n)
    if RUNNING in issue.labels or (run is not None and run.status == "running"):
        raise OrchError(f"issue #{n} is running; wait for its runner to finish")
    return issue


def _text(value: str, name: str) -> str:
    if not value.strip():
        raise OrchError(f"{name} must not be empty")
    return value.strip()


def _event(ctx: RunContext, n: int | None, kind: str, summary: str, *, issues: Iterable[int] = ()) -> dict:
    event = append_event(ctx.store, ctx.now(), n, kind, summary)
    if ctx.store.github_wait_until(ctx.now()) is not None:
        return event
    for number in ([n] if n is not None else issues):
        ctx.tracker.comment(number, "<!-- foragent:orch -->\n" + event["summary"])
    return event


def list_blocked(ctx: RunContext) -> list[dict]:
    """Inspect blocked evidence and audit the lookup, as orch-verbs requires."""
    items = []
    for row in ctx.store.local_issues():
        if row["state"] != "open" or BLOCKED not in row["labels"]:
            continue
        issue = Issue(row["number"], row["title"], row["body"], frozenset(row["labels"]), row["state"])
        comments = [Comment(**item) for item in row["comments"]]
        flags = [(comment, match[1]) for comment in comments
                 if (match := re.fullmatch(r"<!-- foragent:blocked reason=([a-z-]+) -->",
                                           comment.body.splitlines()[0] if comment.body else ""))]
        latest = max(flags, key=lambda item: (datetime.fromisoformat(item[0].created_at), item[0].id), default=None)
        reviews = []
        for path in sorted((ctx.store.root / "runs" / f"issue-{issue.number}").glob("review-*.txt")):
            reviews.append({"path": str(path), "text": path.read_text(encoding="utf-8", errors="replace")})
        log = ctx.store.log_path(issue.number)
        tail = ""
        if log.is_file():
            with log.open(encoding="utf-8", errors="replace") as stream:
                tail = "".join(deque(stream, maxlen=60))
        items.append({"number": issue.number, "title": issue.title, "lane": row["lane"],
                      "reason": latest[1] if latest else None,
                      "blocked_at": latest[0].created_at if latest else None,
                      "details": latest[0].body if latest else "",
                      "reviews": reviews, "log_path": str(log), "log_tail": tail})
    append_event(ctx.store, ctx.now(), None, "list", f"Listed {len(items)} blocked issues")
    return items


def _retry(ctx: RunContext, n: int, hint: str | None) -> None:
    path = ctx.store.root / "hints" / f"{n}.txt"
    if hint is not None:
        ctx.store._atomic_write(path, hint)
    else:
        path.unlink(missing_ok=True)
    if (pr := ctx.tracker.find_pr(f"foragent/issue-{n}")) is not None:
        ctx.tracker.close_pr(pr, "Closed by Foragent orchestrator retry")
    # Establish the destination before clearing the old state, including on errors.
    ctx.tracker.add_labels(n, [READY])
    ctx.tracker.remove_labels(n, [BLOCKED, NEEDS_HUMAN])


def retry(ctx: RunContext, n: int, hint: str | None = None) -> str:
    _issue(ctx, n)
    _retry(ctx, n, hint)
    summary = f"#{n}: queued implementation retry" + (f"; hint: {hint}" if hint else "")
    _event(ctx, n, "retry", summary)
    kick_tick(ctx.repo_root)
    return summary


def rerun_review(ctx: RunContext, n: int) -> str:
    _issue(ctx, n)
    outcome = "interrupted"
    try:
        outcome = review_only(n, ctx)
    finally:
        summary = f"#{n}: reran review on the existing PR; {outcome}"
        _event(ctx, n, "rerun-review", summary)
    return summary


def resolve(ctx: RunContext, n: int, verdict: str, note: str) -> str:
    _issue(ctx, n)
    if verdict not in {"pass", "fail"}:
        raise OrchError("verdict must be pass or fail")
    note = _text(note, "note")
    pr = ctx.tracker.find_pr(f"foragent/issue-{n}")
    if pr is None:
        raise OrchError(f"issue #{n} has no open Foragent PR")
    head = ctx.tracker.pr_head_sha(pr)
    ctx.tracker.comment(n, f"<!-- foragent:orch-verdict pr={pr} head={head} verdict={verdict} -->\n{note}")
    outcome = "interrupted"
    try:
        if verdict == "fail":
            _retry(ctx, n, note)
            outcome = "queued implementation retry"
        else:
            outcome = review_only(n, ctx, verdict_head=head, repair_ci=False)
            # A review verdict cannot fix failing code or CI. Give the implementer
            # the evidence and queue a fresh attempt rather than strand the issue.
            if outcome in {"checks-failed", "ci-failed"}:
                _retry(ctx, n, f"{note}\n\nVerification: {outcome}\nSee {ctx.store.log_path(n)}")
                outcome += "; queued implementation retry"
    finally:
        summary = f"#{n}: resolved review {verdict} for {head}; {outcome}; {note}"
        _event(ctx, n, "resolve", summary)
        kick_tick(ctx.repo_root)
    return summary


def escalate(ctx: RunContext, n: int, summary: str, tried: str,
             options: list[str] | None = None, recommend: int | None = None) -> str:
    _issue(ctx, n)
    summary, tried = _text(summary, "summary"), _text(tried, "tried")
    options = [_text(option, "option") for option in (options or [])]
    if recommend is not None and not 1 <= recommend <= len(options):
        raise OrchError("recommend must be a 1-based option index")
    lines = ["<!-- foragent:needs-human reason=escalated -->", summary, "", "What was tried:", tried]
    if options:
        lines.extend(["", "Options:", *(f"{i}. {option}" + (" (recommended)" if i == recommend else "")
                                           for i, option in enumerate(options, 1))])
    ctx.tracker.comment(n, "\n".join(lines))
    ctx.tracker.add_labels(n, [NEEDS_HUMAN])
    ctx.tracker.remove_labels(n, [READY])
    event_summary = f"#{n}: escalated to human; {summary}; tried: {tried}"
    _event(ctx, n, "escalate", event_summary)
    return event_summary


def log(ctx: RunContext) -> list[dict]:
    """Read the last twenty events, then audit this repository-wide lookup."""
    path = ctx.store.root / "orch-events.jsonl"
    with ctx.store._state_lock():
        if path.exists():
            with path.open(encoding="utf-8") as stream:
                events = [json.loads(line) for line in deque(stream, maxlen=20)]
        else:
            events = []
    _event(ctx, None, "log", f"Read {len(events)} recent orchestrator events")
    return events


def _open_pr(ctx: RunContext, n: int) -> PullRequest:
    pr = next((pr for pr in ctx.tracker.list_prs() if pr.number == n), None)
    if pr is None:
        raise OrchError(f"PR #{n} must be open")
    return pr


@dataclass(frozen=True)
class _Premerge:
    pr: PullRequest
    head: str
    base: str
    report: dict


def _premerge(ctx: RunContext, n: int) -> _Premerge:
    pr = _open_pr(ctx, n)
    head = ctx.tracker.pr_head_sha(n)
    ctx.git.fetch("origin", pr.base)
    # Fetch the exact PR object as well, including heads outside origin's branches.
    ctx.git._call(["fetch", "origin", head])
    base = ctx.git.ref_sha(f"refs/remotes/origin/{pr.base}")
    log_path = ctx.store.root / "runs" / f"premerge-pr-{n}.log"
    with TemporaryDirectory(prefix=f"foragent-premerge-{n}-") as directory:
        worktree = Path(directory) / "worktree"
        ctx.git.add_detached_worktree(worktree, head)
        try:
            publication_conflicts = []
            try:
                ctx.git.at(worktree).check_publication(base)
            except PublicationError as exc:
                publication_conflicts = [f"{path}:{line}" for path, line in exc.markers]
            commands = ([ctx.config.test_command] if ctx.config.test_command is not None
                        else detect_test_commands(worktree))
            if not commands:
                raise OrchError("no full test command detected; set test_command in .foragent.yml")
            tests = "pass"
            for command in commands:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                with log_path.open("a", encoding="utf-8") as stream:
                    stream.write(f"\nPre-merge PR #{n} head={head} base={base}\n$ {command}\n")
                code, timed_out, _ = run_logged(
                    ["/bin/sh", "-c", command], cwd=worktree,
                    timeout_s=ctx.config.max_runtime_minutes * 60, log_path=log_path,
                )
                if code != 0 or timed_out:
                    tests = "fail"
        finally:
            ctx.git.remove_worktree(worktree)
        # Tests may generate files. Use a fresh checkout of the same SHA for
        # the merge probe so those files cannot hide or create conflicts.
        ctx.git.add_detached_worktree(worktree, head)
        try:
            probe = ctx.git.at(worktree)
            conflicts = list(publication_conflicts)
            try:
                probe._call(["-c", "user.name=foragent", "-c", "user.email=foragent@localhost",
                             "merge", "--no-commit", "--no-ff", base])
            except GitError:
                merge_conflicts = sorted(probe.conflict_paths())
                if not merge_conflicts:
                    raise
                conflicts.extend(merge_conflicts)
        finally:
            ctx.git.remove_worktree(worktree)
    ci = ctx.tracker.pr_checks(n)
    report = {"pr": n, "tests": tests, "conflicts": conflicts, "ci": ci,
              "ready": tests == "pass" and not conflicts and ci in {"pass", "none"}}
    return _Premerge(pr, head, base, report)


def premerge(ctx: RunContext, n: int) -> dict:
    """Check the PR head in isolation without posting or changing GitHub state."""
    return _premerge(ctx, n).report


def merge(ctx: RunContext, n: int, *, approved_by: str, at: str | None = None) -> str:
    """Record the human's decision and merge only the freshly checked PR head."""
    approved_by = _text(approved_by, "approved-by")
    at = ctx.now().astimezone(UTC).isoformat() if at is None else _text(at, "at")
    try:
        datetime.fromisoformat(at)
    except ValueError as exc:
        raise OrchError("at must be an ISO8601 timestamp") from exc
    checked = _premerge(ctx, n)
    report = checked.report
    if not report["ready"]:
        raise OrchError(f"PR #{n} is not ready: tests {report['tests']}; "
                        f"conflicts: {', '.join(report['conflicts']) or 'none'}; CI {report['ci']}")
    current = _open_pr(ctx, n)
    ctx.git.fetch("origin", current.base)
    if (current.base != checked.pr.base or current.head != checked.pr.head
            or ctx.git.ref_sha(f"refs/remotes/origin/{current.base}") != checked.base
            or ctx.tracker.pr_head_sha(n) != checked.head):
        raise OrchError(f"PR #{n} head or base changed during pre-merge; run again")
    ci = ctx.tracker.pr_checks(n)
    if ci not in {"pass", "none"}:
        raise OrchError(f"PR #{n} is not ready: CI {ci}")
    ctx.tracker.comment(n, f"Approved by {approved_by} in the orchestrator ({at}). "
                          f"Pre-merge: tests pass, no conflicts, CI {ci}.")
    ctx.tracker.merge_pr(n, expected_head_sha=checked.head)
    summary = f"PR #{n}: merged {checked.head}; approved by {approved_by} at {at}; tests pass, no conflicts, CI {ci}"
    _event(ctx, None, "merge", summary)
    after_merge(ctx, n, current.base)
    if (number := pr_issue_number(current.head)) is not None:
        # GitHub applies closing references only on eligible merges; observe its result.
        try:
            ctx.tracker.get_issue(number)
        except TrackerError as exc:
            print(f"issue #{number} refresh failed after merge: {exc}")
    return summary
