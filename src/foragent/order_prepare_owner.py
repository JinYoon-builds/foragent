"""Who owns an order's preparation, and what status shows when that owner stopped (#1050).

The order's intent (``order_prepare._owner``) reserves one ``fa order prepare`` at a time and can
be reclaimed when that process ends. The record also names the owner by its process
lifetime ``(pid, start_time)``, so a reader can tell a running step from one whose owner stopped
without taking the lock, and a successor takes over only from an owner known to have stopped.
Nothing here signals a process.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import time

from foragent import processes, state_claims
from foragent.config import Config
from foragent.i18n import text
from foragent.file_locks import LockTimeout, acquire, file_lock

# Statuses that only a running owner can be in; after it stopped they are interruptions.
ACTIVE = ("writing_tests", "reviewing_tests")
PUBLICATION_WAIT_S = 10 * 60


def current() -> dict:
    """This process's lifetime as the record names its owner.

    When the kernel start time cannot be read, only the pid is recorded; readers then fall back
    to the existing pid probe, which can keep a reused pid "alive" but never declares a live
    owner dead.
    """
    try:
        row = processes.read_process_identity(os.getpid())
    except PermissionError:
        row = None
    return {"pid": os.getpid(), "start_time": row.start_time if row is not None else None,
            "taken_at": datetime.now(UTC).isoformat(timespec="seconds")}


def alive(owner: dict | None) -> bool | None:
    """Whether the recorded owner still runs: True, False, or None when that cannot be known.

    A recorded lifetime is checked by a fresh identity read (None when unreadable). A pid without
    a start time uses the existing liveness rule (signal 0 only; a denied probe means alive). An
    owner entry that names no pid is unknown. Only False is evidence that the owner stopped.
    """
    from foragent import capacity

    if not isinstance(owner, dict) or type(owner.get("pid")) is not int or owner["pid"] <= 0:
        return None
    if not isinstance(owner.get("start_time"), (int, float)):
        return capacity._alive(owner["pid"])
    return processes.recorded_identity_evidence(owner["pid"], owner["start_time"], fresh=True).alive


def _same(owner: dict | None, me: dict) -> bool:
    """A recorded owner with this process's pid is this process or one that already ended."""
    return isinstance(owner, dict) and owner.get("pid") == me["pid"]


@contextmanager
def publication_gate(folder: Path):
    """Serialize route-record transitions and the legacy direct publication path.

    The lock lives in the repository's state folder, which every clone on this machine shares;
    each clone's own publication lock does not reach the others. Publications wait for each other.
    """
    with file_lock(folder / "route.lock", journal_root=folder.parent):
        yield


@contextmanager
def reserve_routes(folder: Path, resources, *, held=None):
    """Reserve order/key routes (or existing issue numbers) without a kernel hold.

    Preparation holds these only through its durable claim. Publication keeps
    them from its last local route check through issue completion. Unknown or
    live owners defer; a stopped owner can be recovered by either path.
    """
    resources = {("", key) if key.startswith("#") else (identity, key) for identity, key in resources}
    held = set() if held is None else held
    resources -= held  # One publication batch can contain multiple revisions of a route.
    with ExitStack() as stack:
        for resource in sorted(resources):
            digest = sha256(json.dumps(resource, ensure_ascii=False).encode()).hexdigest()
            if not stack.enter_context(state_claims.hold(folder / "routes" / f"{digest}.intent")):
                yield False
                return
        held.update(resources)
        try:
            yield True
        finally:
            held.difference_update(resources)


def claim(folder: Path, record: dict, keys: list[str], write, aliases=(), *, wait_s: float = PUBLICATION_WAIT_S,
          clock=None, sleep=None) -> bool:
    """Take the statement route before any agent starts, waiting boundedly for publication.

    Under the gate, so a publication either sees the claim at its last check or finishes first.
    A timeout reports waiting and starts nothing; no claim is made outside the gate.
    """
    clock, sleep = clock or time.monotonic, sleep or time.sleep
    folder.mkdir(parents=True, exist_ok=True)
    with reserve_routes(folder, [*((record.get("plan", ""), key) for key in keys), *aliases]) as owned:
        if not owned:
            return False
        return _claim(folder, record, keys, write, wait_s, clock, sleep)


def _claim(folder, record, keys, write, wait_s, clock, sleep):
    with open(folder / "route.lock", "a+") as handle:
        try:
            acquire(handle.fileno(), folder / "route.lock", timeout=wait_s, journal_root=folder.parent,
                    clock=clock, sleep=sleep)
        except LockTimeout:
            return False
        try:
            record["claimed"] = sorted({*record.get("claimed", []), *keys})
            write()
            return True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def take(record: dict, config: Config) -> bool:
    """Become the record's owner while holding the order's lock; False while another owner may run.

    A recorded owner that is not this process keeps its preparation unless it is known to have
    stopped: a running or unknown liveness is never treated as death.
    Otherwise the steps it left running are closed as interrupted, with their facts kept, so the
    next run resumes them: an unfinished review is reviewed again from the recorded candidate
    bytes, an unfinished writer is written again and its partial files are never candidates.
    """
    me = current()
    previous = record.get("owner")
    # The intent excludes another holder, but the file can be replaced under a
    # live owner; only evidence that the recorded owner stopped lets this process take over.
    if previous is not None and not _same(previous, me) and alive(previous) is not False:
        return False
    now = datetime.now(UTC).isoformat(timespec="seconds")
    for entry in record.get("issues", {}).values():
        if not isinstance(entry, dict) or entry.get("status") not in ACTIVE:
            continue
        reason = text("prepare_owner_stopped", config.language, phase=entry["status"])
        for attempt in entry.get("attempts", []):
            if isinstance(attempt, dict) and not attempt.get("finished_at"):
                attempt.update(status="interrupted", reason=reason, finished_at=now)
        entry.update(phase=entry["status"], status="interrupted", reason=reason, updated_at=now)
    record["owner"] = me
    return True


def project(entry: dict, owner: dict | None, config: Config) -> dict:
    """The entry as status shows it: a running step whose owner is known to have stopped is interrupted.

    Read only: the record is not changed; the next owner records the interruption itself.
    """
    if entry.get("status") not in ACTIVE or alive(owner) is not False:
        return entry
    return {**entry, "phase": entry["status"], "status": "interrupted",
            "reason": text("prepare_owner_stopped", config.language, phase=entry["status"])}


def row(key: str, entry: dict | None, config: Config, owner: dict | None = None) -> dict:
    """One issue's reported facts: status, phase, reason, time, base, holds and the evidence there is."""
    from foragent.order_prepare_cycle import holds

    entry = project(entry, owner, config) if entry else {
        "status": "not_prepared", "reason": text("prepare_none", config.language)}
    result = {"key": key, "status": entry["status"], "phase": entry.get("phase"),
              "questions": entry.get("questions", []), "notes": entry.get("notes", []), "holds": len(holds(entry)),
              "reason": entry.get("reason"), "updated_at": entry.get("updated_at"),
              "base_revision": entry.get("base_revision")}
    for name in ("scratch", "done_when", "ignored", "red", "coverage"):
        if entry.get(name):
            result[name] = entry[name]
    if entry.get("written"):
        result["written"] = sorted(entry["written"])
    if entry.get("attempts"):
        result["attempts"] = len(entry["attempts"])
    return result
