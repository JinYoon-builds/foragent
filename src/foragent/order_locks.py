"""Approved issues whose locked acceptance bytes a publication would change (#1107).

An approval snapshot locks the Git blob of every acceptance file the issue relies
on; the gate flags hash-mismatch when the base's blob differs. Publication lists
every approved issue whose locked bytes it would change. It may go ahead only when
each of them is re-approved in the same publication; otherwise it refuses.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable

from foragent.config import Config
from foragent.git import GitRepo
from foragent.i18n import text
from foragent.issue import acceptance_paths, parse_body
from foragent.state import StateStore
from foragent.tracker import Issue, Tracker


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


def lines(affected: dict[int, list[str]]) -> list[str]:
    return [f"#{number}: {', '.join(paths)}" for number, paths in affected.items()]


def refusal(affected: dict[int, list[str]], language: str) -> str:
    return text("order_locks_refused", language, owners="\n".join(lines(affected)))
