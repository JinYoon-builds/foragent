"""Approved issues whose locked acceptance bytes a publication would change (#1107).

An approval snapshot locks the Git blob of every acceptance file the issue relies
on; the gate flags hash-mismatch when the base's blob differs. Publication lists
every approved issue whose locked bytes it would change. It may go ahead only when
each of them is re-approved in the same publication; otherwise it refuses.
"""

from __future__ import annotations

import hashlib
import time
from datetime import datetime
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager

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
    approved_ref = git().commit_before(f"origin/{config.base_branch}", event.created_at)
    return git().blob_shas(approved_ref, paths) if approved_ref is not None else {}


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


GATE_WAIT_S = 15 * 60
APPROVAL_ATTEMPTS = 5


@contextmanager
def gate_paused(repo: str, busy: Callable[[], Exception]) -> Iterator[None]:
    """Hold this machine's tick lock, waiting up to ``GATE_WAIT_S`` for a running tick.

    A tick lists issues and checks their approvals against the base it fetched; on
    this machine the publication moves that same base. Holding the lock, no tick
    runs between the approval withdrawal, the push and the re-approval. Ticks of
    other machines fetch the base before they list issues, so they see either the
    old bytes or the withdrawn approval. The lock is taken on its own descriptor,
    so even a tick on this thread finds it busy.
    """
    store = StateStore.for_repo(repo)
    path = store._tick_lock_path()
    deadline = time.monotonic() + GATE_WAIT_S
    while True:
        with store._try_tick_lock(path) as acquired:
            if acquired:
                yield
                return
        if time.monotonic() >= deadline:
            raise busy()
        time.sleep(1)


def _approval_times(tracker: Tracker, number: int) -> list[datetime]:
    return [datetime.fromisoformat(event.created_at) for event in tracker.label_events(number)
            if event.label == "approved" and event.action == "labeled"]


def approve_after_edits(tracker: Tracker, number: int, approvers: list[str], language: str) -> None:
    """Add ``approved`` so the gate reads it as a new approval, later than the last body edit.

    GitHub times have whole seconds. The gate refuses an approval whose time is not
    after the last body edit, and it keeps a snapshot whose time equals the latest
    approval's, so the new approval must also be later than every earlier one. The
    label is read back; an approval in such a second is withdrawn and added again a
    second later.
    """
    from foragent import scheduler_approval

    earlier = max(_approval_times(tracker, number), default=None)
    for _ in range(APPROVAL_ATTEMPTS):
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


def confirm_locked(tracker: Tracker, git: GitRepo, config: Config, shared: dict[int, list[str]],
                   published: dict[str, str]) -> None:
    """Check, on a fresh base, that each re-approved owner is held to the published bytes.

    The gate locks an approval to the base at the approval time. A publication of
    another machine may have replaced the tests between this push and the
    re-approval; those owners would then be approved for bytes nobody reviewed, so
    their approval is withdrawn and the publication reports them.
    """
    from foragent import scheduler_approval

    git.fetch("origin", config.base_branch)
    replaced: dict[int, list[str]] = {}
    for number, paths in shared.items():
        event, _ = scheduler_approval._latest_approval(tracker.label_events(number), config.approvers)
        if event is None or "approved" not in tracker.get_issue(number).labels:
            continue
        ref = git.commit_before(f"origin/{config.base_branch}", event.created_at)
        locked = git.blob_shas(ref, paths) if ref is not None else {}
        wrong = sorted(path for path in paths if locked.get(path) != published[path])
        if wrong:
            replaced[number] = wrong
    for number in replaced:
        tracker.remove_labels(number, ["approved", "ready"])
    if replaced:
        raise TrackerError(text("order_locks_replaced", config.language, owners="\n".join(lines(replaced))))


def lines(affected: dict[int, list[str]]) -> list[str]:
    return [f"#{number}: {', '.join(paths)}" for number, paths in affected.items()]


def refusal(affected: dict[int, list[str]], language: str) -> str:
    return text("order_locks_refused", language, owners="\n".join(lines(affected)))
