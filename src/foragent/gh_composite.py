"""Finish a gh command that sends several writes after a refused attempt (#1208).

``issue close --comment``, ``pr close --delete-branch [--comment]`` and
``pr merge --delete-branch`` each send several requests. A 401 can arrive after
an earlier request took effect, so the same argv is never sent twice. After a
refused first attempt the tracker reads what already happened and sends only
the remaining effects, each as one request that the transport may retry.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from typing import TYPE_CHECKING
from urllib.parse import quote

if TYPE_CHECKING:
    from foragent.tracker import GhTracker

# GitHub's clock and ours may differ a little; an older identical comment is not ours.
_CLOCK_SKEW = timedelta(minutes=2)


def combines_effects(args: list[str]) -> bool:
    command = args[:2]
    return (command == ["issue", "close"] and "--comment" in args
            or command in (["pr", "close"], ["pr", "merge"]) and ("--comment" in args or "--delete-branch" in args))


def _option(args: list[str], name: str) -> str | None:
    return args[args.index(name) + 1] if name in args else None


def _comment_once(tracker: GhTracker, n: int, body: str | None, started: datetime) -> None:
    if body is None:
        return
    since = started - _CLOCK_SKEW
    for comment in tracker.list_comments(n):
        try:
            created = datetime.fromisoformat(comment.created_at.replace("Z", "+00:00"))
        except (AttributeError, ValueError):
            continue
        if created >= since and comment.body.strip() == body.strip():
            return  # The refused attempt already posted it.
    tracker.comment(n, body)


def _delete_branch(tracker: GhTracker, head: str) -> None:
    from foragent.tracker import TrackerError

    try:
        tracker._call(["api", "-X", "DELETE", f"{tracker._path}/git/refs/heads/{quote(head, safe='/')}"])
    except TrackerError as exc:
        # 404/422: the refused attempt (or GitHub's auto-delete) already removed it.
        if not (exc.is_not_found or "Reference does not exist" in str(exc)):
            raise


def finish(tracker: GhTracker, args: list[str], started: datetime | None = None) -> None:
    """Send the effects of ``args`` that a refused attempt did not complete."""
    started = started or datetime.now(UTC)
    number = args[2]
    comment = _option(args, "--comment")
    if args[:2] == ["issue", "close"]:
        _comment_once(tracker, int(number), comment, started)
        if tracker.get_issue(int(number)).state != "closed":
            tracker._call(["issue", "close", number, "-R", tracker.repo])
        return
    view = json.loads(tracker._call(["pr", "view", number, "-R", tracker.repo, "--json", "state,headRefName"]))
    _comment_once(tracker, int(number), comment, started)
    if args[:2] == ["pr", "close"]:
        if view["state"] == "OPEN":
            tracker._call(["pr", "close", number, "-R", tracker.repo])
    elif view["state"] != "MERGED":
        tracker._call([arg for arg in args if arg != "--delete-branch"])
    if "--delete-branch" in args:
        _delete_branch(tracker, view["headRefName"])
