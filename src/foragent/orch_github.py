"""Persist GitHub availability waits independently of issue failures."""

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Protocol

from foragent.state import StateStore
from foragent.tracker import Tracker, TrackerError


class WaitContext(Protocol):
    tracker: Tracker
    store: StateStore
    now: Callable[[], datetime]


def pause(ctx: WaitContext, error: TrackerError) -> datetime:
    """Prefer response headers; probe once otherwise. An outage waits a minute, unprobed, with its cause."""
    until = error.reset_at
    if until is None and error.is_rate_limit:
        try:
            until = ctx.tracker.rate_limit_reset()
            remaining = getattr(ctx.tracker, "rate_limit_remaining", None)
        except (TrackerError, ValueError, TypeError, AttributeError, OverflowError, OSError):
            # A failed reset lookup must not turn throttling into an issue failure.
            until = remaining = None
        if type(remaining) is int:
            # The same response's count, timed by the local clock when it arrived (D93 facts).
            ctx.store.record_github_remaining(remaining, ctx.now())
    now = ctx.now()
    if until is None or until <= now:
        until = now + timedelta(minutes=1)
    return ctx.store.pause_github(until, None if error.is_rate_limit else cause(error))


def cause(error: TrackerError) -> str:
    """One line of the actual failure for a wait's log and run detail."""
    return error.diagnostic[:500]


def _line(until: datetime, outage: str | None) -> str:
    return f"GitHub unavailable ({outage}) until {until.isoformat()}" if outage else (
        f"GitHub rate limit until {until.isoformat()}")


def wait_text(store: StateStore, now: datetime, error: TrackerError | None = None) -> str | None:
    """What a wait is for. A caller's own failure names it; another writer may replace the shared
    pause's cause meanwhile. Only a run or tick that merely meets the pause reads that record."""
    if (until := store.github_wait_until(now)) is None:
        return None
    outage = store.github_pause_cause() if error is None else None
    return _line(until, outage if error is None else None if error.is_rate_limit else cause(error))


def wait_run(ctx: WaitContext, number: int, until: datetime, error: TrackerError | None = None) -> str:
    """Record a resumable wait, for ``error`` or for the pause it met, in the log and the run detail."""
    line = wait_text(ctx.store, ctx.now(), error) or _line(until, None if error is None or error.is_rate_limit
                                                           else cause(error))
    ctx.store.update_run(number, waiting_until=until.isoformat(), outcome="limit-wait", detail=line)
    log = ctx.store.log_path(number)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as stream:
        stream.write(f"Waiting: {line}\n")
    return "limit-wait"


def pending_push(ctx, number: int) -> dict | None:
    """Latest uninterrupted push wait; completion/refusal consumes its old lease."""
    from foragent.orch_events import read_events

    pending = None
    for event in read_events(ctx.store):
        if event.get("issue") != number:
            continue
        if event.get("kind") == "push-wait":
            pending = event
        elif event.get("kind") == "push-end":
            pending = None
    return pending


def _push_event(ctx, number: int, kind: str, **fields) -> None:
    from foragent.orch_events import append_event

    append_event(ctx.store, ctx.now(), number, kind, "Git push " + kind, **fields)


def wait_out_push(ctx, number: int, do_push, *, head: str, lease: str | None, branch: str,
                  kind: str) -> None:
    """One push attempt, retaining the exact verified head and lease across outage waits."""
    from foragent.git import GitError, push_server_error

    if getattr(ctx, "merge_owner_started_at", None) is not None:
        do_push()  # The merge owner's durable publishing job retries on its next tick.
        return
    previous = pending_push(ctx, number)
    started = (previous["started"] if previous and previous["head"] == head
               else ctx.now().isoformat())
    try:
        do_push()
    except GitError as exc:
        if not push_server_error(str(exc)) or (
            ctx.now() - datetime.fromisoformat(started)
        ).total_seconds() >= ctx.config.transient_max_wait_minutes * 60:
            _push_event(ctx, number, "push-end", head=head)
            raise
        from foragent import review_pass_scope

        approval = ctx.store.load_approval(number)
        heads = review_pass_scope._heads(number, ctx)
        reviewed = kind == "refresh" or any(
            (event.get("head") or heads.get(event["tree"])) == head
            and event.get("body_sha") == approval.body_sha
            for event in review_pass_scope._passes(number, ctx))
        _push_event(ctx, number, "push-wait", head=head, lease=lease, branch=branch,
                    push_kind=kind, started=started, body_sha=approval.body_sha,
                    approved_at=approval.approved_at, reviewed=reviewed)
        raise TrackerError(str(exc), transport=True) from exc
    else:
        if previous:
            _push_event(ctx, number, "push-end", head=head)


def resume_push(ctx, number: int, git) -> bool | None:
    """Repeat a pending push: None means no push; bool says whether its review already finished."""
    from foragent.git import GitError
    from foragent import run_publish
    from foragent.run_attempt import ExecutorStopped

    pending = pending_push(ctx, number)
    if pending is None:
        return None
    approval = ctx.store.load_approval(number)
    if (git.head_sha() != pending["head"] or approval.body_sha != pending["body_sha"]
            or approval.approved_at != pending["approved_at"]
            or git._call(["status", "--porcelain", "--untracked-files=no"]).strip()):
        _push_event(ctx, number, "push-end", head=pending["head"])
        return None
    branch, lease = pending["branch"], pending["lease"]
    try:
        # Resume through the original push sites, so no new push path exists.
        if pending["push_kind"] == "refresh":
            from foragent.run_publish_reviewed import push_refresh

            push_refresh(ctx, number, git, branch, lease, pending["head"])
        else:
            from foragent.run_publish_pr import push_publication

            push_publication(ctx, number, git, branch, lease)
    except GitError as exc:
        if lease is None or "(stale info)" not in str(exc):
            raise
        raise ExecutorStopped(run_publish._hold_changed_head(
            number, ctx, f"https://github.com/{ctx.repo}/tree/{branch}")) from exc
    return pending["reviewed"]
