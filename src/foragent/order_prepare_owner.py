"""Who owns an order's preparation, and what status shows when that owner stopped (#1050).

The order's lock (``order_prepare._owner``) is held by one ``fa order prepare`` at a time and the
kernel releases it when that process ends. The record also names the owner by its process
lifetime ``(pid, start_time)``, so a reader can tell a running step from one whose owner stopped
without taking the lock, and a successor takes over only from an owner known to have stopped.
Nothing here signals a process.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
import fcntl
import os
from pathlib import Path

from foragent import processes
from foragent.config import Config
from foragent.i18n import text

# Statuses that only a running owner can be in; after it stopped they are interruptions.
ACTIVE = ("writing_tests", "reviewing_tests")


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
    """Hold publication's last check and its writes as one step against any preparation starting.

    The lock lives in the repository's state folder, which every clone on this machine shares;
    each clone's own publication lock does not reach the others. Publications wait for each other.
    """
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / "route.lock", "a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def claim(folder: Path, record: dict, keys: list[str], write) -> bool:
    """Take the statement route for ``keys`` before any agent starts; False while a publication runs.

    Under the gate, so a publication either sees the claim at its last check or finishes first.
    Preparation never waits here: it reports waiting and starts nothing.
    """
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / "route.lock", "a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
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
    # The lock proves no other holder of this lock file runs, but the file can be replaced under a
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
              "questions": entry.get("questions", []), "holds": len(holds(entry)),
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
