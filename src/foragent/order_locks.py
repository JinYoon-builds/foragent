"""Approved issues whose locked acceptance bytes a publication would change (#1107).

An approval snapshot locks the Git blob of every acceptance file the issue relies
on; the gate flags hash-mismatch when the base's blob differs. Publication lists
every approved issue whose locked bytes it would change. It may go ahead only when
each of them is re-approved in the same publication; otherwise it refuses. A
re-approval records the published blobs (``approval_record``).
"""

from __future__ import annotations

import hashlib
import time
from datetime import datetime
from collections.abc import Callable, Iterable

from foragent import approval_record
from foragent.config import Config
from foragent.git import GitRepo
from foragent.i18n import text
from foragent.issue import acceptance_paths, parse_body
from foragent.state import StateStore
from foragent.tracker import Issue, Tracker, TrackerError


def blob(data: bytes, like: str) -> str:
    """Git's blob id of ``data`` in the object format of the id ``like``."""
    digest = hashlib.sha256 if len(like) == 64 else hashlib.sha1
    return digest(b"blob %d\0" % len(data) + data).hexdigest()


def _gate_lock(issue: Issue, approval, paths: list[str], config: Config, tracker: Tracker,
               git: Callable[[], GitRepo]) -> dict[str, str]:
    """The blobs the gate holds this approval to, among ``paths`` and the snapshot's.

    Mirrors the gate (``scheduler`` and ``scheduler_approval._snapshot``): only the
    latest approval by an approver counts. A stored snapshot of that same approval
    is the lock. Otherwise, with no snapshot or one left over from an earlier
    approval the gate has not replaced yet, the gate will lock the Done-when paths
    at the last base commit before the approval time. An approval the gate would
    not accept, or cannot place on the base, locks nothing.
    """
    from foragent import scheduler_approval

    event, _ = scheduler_approval._latest_approval(tracker.label_events(issue.number), config.approvers)
    if event is None:
        return {}
    if approval is not None and approval.approved_at == event.created_at:
        return dict(approval.acceptance_blobs)
    if not paths:
        return {}
    # A recorded hash wins over the approval-time lookup, as in ``scheduler_approval``.
    record = {path: sha for path, sha in approval_record.recorded(
        tracker.label_events(issue.number), tracker.list_comments(issue.number), event).items() if path in paths}
    looked_up = [path for path in paths if path not in record]
    if not looked_up:
        return record
    approved_ref = git().commit_before(f"origin/{config.base_branch}", event.created_at)
    return {**(git().blob_shas(approved_ref, looked_up) if approved_ref is not None else {}), **record}


def affected_owners(config: Config, repo: str, tracker: Tracker, issues: Iterable[Issue],
                    files: dict[str, str | bytes], git: Callable[[], GitRepo]) -> dict[int, list[str]]:
    """``{issue number: changed paths}`` for every open approved issue, in this order or not.

    The locked blobs are the ones the gate will check the issue against (``_gate_lock``).
    Paths whose new bytes equal the locked bytes are not affected.
    """
    store = StateStore.for_repo(repo)
    new = {name: data.encode("utf-8") if isinstance(data, str) else data for name, data in files.items()}
    affected: dict[int, list[str]] = {}
    for issue in issues:
        if issue.state != "open" or "approved" not in issue.labels:
            continue
        approval = store.load_approval(issue.number)
        paths = [name for name in acceptance_paths(parse_body(issue.body).done_when_commands,
                                                   config.acceptance_dir) if name in new]
        if not paths and not (approval is not None and set(approval.acceptance_blobs) & new.keys()):
            continue  # Nothing this publication writes can be locked for this issue.
        locked = _gate_lock(issue, approval, paths, config, tracker, git)
        changed = sorted(name for name, sha in locked.items() if name in new and blob(new[name], sha) != sha)
        if changed:
            affected[issue.number] = changed
    return dict(sorted(affected.items()))


APPROVAL_ATTEMPTS = 5


def _approval_times(tracker: Tracker, number: int) -> list[datetime]:
    return [datetime.fromisoformat(event.created_at) for event in tracker.label_events(number)
            if event.label == "approved" and event.action == "labeled"]


def record_for(tracker: Tracker, number: int, config: Config, published: dict[str, str]) -> dict[str, str]:
    """The published blobs of the locked files the gate will check for ``number``."""
    paths = acceptance_paths(parse_body(tracker.get_issue(number).body).done_when_commands, config.acceptance_dir)
    return {path: published[path] for path in paths if path in published}


def approve_after_edits(tracker: Tracker, number: int, approvers: list[str], language: str,
                        record: dict[str, str]) -> None:
    """Add ``approved`` so the gate reads it as a new approval, later than the last body edit.

    GitHub times have whole seconds. The gate refuses an approval whose time is not
    after the last body edit, and it keeps a snapshot whose time equals the latest
    approval's, so the new approval must also be later than every earlier one. The
    label is read back; an approval in such a second is withdrawn and added again a
    second later. ``record`` (the approved blob hashes) is written right before each
    attempt, so it belongs to that approval.
    """
    from foragent import scheduler_approval

    earlier = max(_approval_times(tracker, number), default=None)
    for _ in range(APPROVAL_ATTEMPTS):
        # Written before each attempt, so the record precedes exactly that approval.
        if record:
            tracker.comment(number, approval_record.comment_body(record))
        tracker.add_labels(number, ["approved"])
        event, _ = scheduler_approval._latest_approval(tracker.label_events(number), approvers)
        if event is None:
            return  # The gate does not accept this publisher's approval; nothing it could misread.
        at, edited = datetime.fromisoformat(event.created_at), tracker.body_last_edited_at(number)
        if (edited is None or at > datetime.fromisoformat(edited)) and (earlier is None or at > earlier):
            return
        tracker.remove_labels(number, ["approved"])
        time.sleep(1)
    raise TrackerError(text("order_locks_approval_unordered", language, number=number))


def lines(affected: dict[int, list[str]]) -> list[str]:
    return [f"#{number}: {', '.join(paths)}" for number, paths in affected.items()]


def refusal(affected: dict[int, list[str]], language: str) -> str:
    return text("order_locks_refused", language, owners="\n".join(lines(affected)))
