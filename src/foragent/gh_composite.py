"""Report a refused gh command that sends several writes truthfully (#1208).

``issue close --comment``, ``pr close --delete-branch [--comment]`` and
``pr merge --delete-branch`` each send several requests, and a 401 can arrive
after an earlier one took effect. Such a command is never sent again, and it is
never rewritten into other writes (a merge without branch deletion could only
enqueue; a guessed branch name can belong to a fork). After a refusal the
tracker re-reads the one fact the caller asked for (is the issue closed? is the
PR closed or merged?) with reads that the transport may retry. If it holds, the
command did its job; otherwise the original authentication error stands.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from foragent.tracker import GhTracker, TrackerError


def combines_effects(args: list[str]) -> bool:
    command = args[:2]
    return (command == ["issue", "close"] and "--comment" in args
            or command in (["pr", "close"], ["pr", "merge"]) and ("--comment" in args or "--delete-branch" in args))


def settled(tracker: GhTracker, args: list[str]) -> bool:
    """Whether GitHub already shows the effect ``args`` was sent for (reads only)."""
    number = args[2]
    if args[:2] == ["issue", "close"]:
        return tracker.get_issue(int(number)).state == "closed"
    state = json.loads(tracker._call(["pr", "view", number, "-R", tracker.repo, "--json", "state"]))["state"]
    return state == "MERGED" if args[:2] == ["pr", "merge"] else state != "OPEN"


def recover(tracker: GhTracker, args: list[str], refused: TrackerError) -> None:
    """Accept the refused command only if its effect is visible; never resend a write."""
    if not settled(tracker, args):
        raise refused
