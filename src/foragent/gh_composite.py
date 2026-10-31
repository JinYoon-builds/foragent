"""Send a gh command that makes several writes, recovering from refusals (#1208).

``issue close --comment``, ``pr close --delete-branch [--comment]`` and
``pr merge --delete-branch`` each send several requests, and a 401 can arrive
after an earlier one took effect. After a refusal the tracker waits and reads
what GitHub shows, with reads that the transport may retry:

- ``done``: the requested effect is visible *for this operation* (a merge
  counts only at the exact ``--match-head-commit`` head), so the call succeeded;
- ``none``: no write of this command is visible, so the same argv is sent again
  within the authentication budget;
- ``partial``: some write is visible but not the requested effect (a comment of
  ours on a still-open issue, a PR merged or closed some other way). Nothing is
  resent or rewritten, and the refusal stands.

An unreadable state proves nothing, so the refusal also stands.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
import json
from typing import TYPE_CHECKING

from foragent import auth_retry

if TYPE_CHECKING:
    from foragent.tracker import GhTracker

# GitHub's clock and ours may differ a little.
_CLOCK_SKEW = timedelta(minutes=2)


def combines_effects(args: list[str]) -> bool:
    command = args[:2]
    return (command == ["issue", "close"] and "--comment" in args
            or command in (["pr", "close"], ["pr", "merge"]) and ("--comment" in args or "--delete-branch" in args))


def _option(args: list[str], name: str) -> str | None:
    return args[args.index(name) + 1] if name in args else None


def _our_comment_posted(tracker: GhTracker, n: int, body: str | None, started: datetime) -> bool:
    """Whether this tracker's own login posted ``body`` on ``n`` since the attempt began."""
    if body is None:
        return False
    login = tracker.authenticated_login().lower()
    since = started - _CLOCK_SKEW
    for comment in tracker.list_comments(n):
        created = datetime.fromisoformat(comment.created_at.replace("Z", "+00:00"))
        if comment.author.lower() == login and created >= since and comment.body.strip() == body.strip():
            return True
    return False


def observe(tracker: GhTracker, args: list[str], started: datetime) -> str:
    """``done``, ``none`` or ``partial`` for ``args`` (reads only)."""
    number = args[2]
    comment = _option(args, "--comment")
    if args[:2] == ["issue", "close"]:
        if tracker.get_issue(int(number)).state == "closed":
            return "done"
        return "partial" if _our_comment_posted(tracker, int(number), comment, started) else "none"
    view = json.loads(tracker._call(["pr", "view", number, "-R", tracker.repo, "--json", "state,headRefOid"]))
    state = view["state"]
    if args[:2] == ["pr", "merge"]:
        if state == "MERGED":
            # Only this operation's merge: the head it was allowed to merge.
            return "done" if view.get("headRefOid") == _option(args, "--match-head-commit") else "partial"
        return "partial" if state != "OPEN" else "none"
    if state == "CLOSED":
        return "done"
    if state != "OPEN":
        return "partial"  # merged meanwhile: not the close this operation asked for
    return "partial" if _our_comment_posted(tracker, int(number), comment, started) else "none"


def send(tracker: GhTracker, args: list[str], attempt: Callable[[], object]) -> object:
    """Send ``args``; after a refusal resend it only while GitHub shows none of its writes."""
    from foragent.tracker import TrackerError

    started = datetime.now(UTC)
    for tries in range(auth_retry.ATTEMPTS):
        try:
            return attempt()
        except TrackerError as exc:
            if not exc.is_auth_failure:
                raise
            refused = exc
        tracker._sleep(auth_retry.WAITS[min(tries, len(auth_retry.WAITS) - 1)])
        try:
            state = observe(tracker, args, started)
        except (TrackerError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise refused from exc
        if state == "done":
            return ""
        if state == "partial":
            raise refused
    raise refused
