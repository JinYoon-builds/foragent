"""Is a blocker finished? Its Done when on main decides, not its close reason (Jin r1301, PM r1334).

``finished`` answers for one blocker of an approved issue, whatever the close reason says:

- closed and its Done when passes on main: finished;
- closed and its Done when fails on main: not finished, and the orchestrator gets one request
  saying it closed without delivering;
- open: not finished; when its Done when already passes on main, the orchestrator gets one
  request saying it looks delivered, so it can close it.

Only a blocker that has no Done when to run (another repository, unreadable issue, no machine
check) keeps the close-reason rule, returned as ``None``: there is no delivery proof to read.

The proof never runs under the tick lock. A worker process (``python -m`` this module) checks out
main, takes a machine test slot and runs the Done when; it alone writes
``blocker-proofs/<N>.json`` (keyed by main SHA and body SHA). A tick reads that record, starts the
worker when it is missing, and waits for it at most ``JOIN_SECONDS`` in total per tick; an
unfinished proof holds the waiting issue until a later tick reads it.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from foragent import orch_inbox
from foragent.git import GitError
from foragent.issue import has_machine_check, parse_body
from foragent.tracker import Dependency, TrackerError

if TYPE_CHECKING:
    from foragent.scheduler import TickContext

PROOF_SECONDS = 900   # one Done when command, in the worker
JOIN_SECONDS = 30     # all waiting for workers inside one tick
PENDING = "pending"   # the proof is not known yet: hold, tell nobody
_LAUNCHED: dict[tuple[str, int, str], subprocess.Popen] = {}
_TICK: dict[str, object] = {}


def _folder(root: Path) -> Path:
    return Path(root) / "blocker-proofs"


def _result(root: Path, number: int) -> dict:
    try:
        return json.loads((_folder(root) / f"{number}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _busy(lock: Path) -> bool:
    """A worker holds the blocker's lock while it runs."""
    try:
        with open(lock, "a") as stream:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            return False
    except OSError:
        return True


def _left(report: object) -> float:
    """Seconds this tick may still wait for workers; ``report`` is new each tick."""
    if _TICK.get("report") is not report:
        _TICK.update(report=report, until=time.monotonic() + JOIN_SECONDS)
    return max(0.0, float(_TICK["until"]) - time.monotonic())


def _proof(ctx: TickContext, report: object, number: int, commands: list[str], main: str, body: str):
    key = f"{main}:{body}"
    root = ctx.store.root
    known = _result(root, number)
    if known.get("key") == key and isinstance(known.get("passed"), bool):
        return known["passed"]
    folder = _folder(root)
    folder.mkdir(parents=True, exist_ok=True)
    slot = (str(root), number, key)
    process = _LAUNCHED.get(slot)
    if process is None and not _busy(folder / f"{number}.lock"):
        request = folder / f"{number}.request.json"
        request.write_text(json.dumps({"key": key, "main": main, "commands": commands}), encoding="utf-8")
        try:
            process = subprocess.Popen(
                [sys.executable, "-m", __name__, str(root), str(ctx.git.root), str(number)],
                cwd=str(root), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True,
            )
        except OSError:
            return PENDING
        _LAUNCHED[slot] = process
    if process is not None and (left := _left(report)) > 0:
        try:
            process.wait(timeout=left)
        except subprocess.TimeoutExpired:
            pass
    known = _result(root, number)
    if known.get("key") == key and isinstance(known.get("passed"), bool):
        _LAUNCHED.pop(slot, None)
        return known["passed"]
    if process is not None and process.poll() is not None:
        _LAUNCHED.pop(slot, None)  # It ended without a verdict: a later tick starts another.
    return PENDING


def passes_on_main(ctx: TickContext, report: object, dependency: Dependency):
    """True or False from the blocker's Done when on main; PENDING while the proof is unknown;
    None when the blocker has no Done when to run."""
    if dependency.repository is not None:
        return None
    try:
        body = ctx.tracker.get_issue(dependency.number).body
    except KeyError:
        return None
    except TrackerError as exc:
        if exc.waits:
            raise
        return PENDING
    parsed = parse_body(body or "")
    if not has_machine_check(parsed) or not parsed.done_when_commands:
        return None
    try:
        main = ctx.git.ref_sha(f"origin/{ctx.config.base_branch}")
    except (GitError, OSError, AttributeError):
        return PENDING  # Main cannot be read now: no verdict, so nothing starts.
    body_sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return _proof(ctx, report, dependency.number, list(parsed.done_when_commands), main, body_sha)


def _tell_once(ctx: TickContext, key: str, text: str, fields: dict) -> None:
    sent = {row.get("blocker_key") for row in orch_inbox.requests(ctx.store) if row.get("from") == "gate"}
    if key not in sent:
        orch_inbox.add_request(ctx.store, "gate", text, {"blocker_key": key, **fields})


def finished(ctx: TickContext, report: object, waiting: int, dependency: Dependency) -> bool | None:
    """True: the blocker no longer holds ``waiting``. False: it holds. None: use the close reason."""
    passed = passes_on_main(ctx, report, dependency)
    blocker = dependency.number
    if dependency.state == "open":
        if passed is True:
            _tell_once(ctx, f"looks-delivered:{blocker}", (
                f"#{blocker} looks delivered: its Done when already passes on main while it is still "
                f"open, and #{waiting} waits on it. Close #{blocker} as delivered; the gate then "
                f"starts #{waiting}."), {"issue": blocker, "waiting": waiting})
        return False
    if passed is None:
        return None
    if passed is PENDING:
        return False
    if not passed:
        _tell_once(ctx, f"closed-undelivered:{waiting}:{blocker}", (
            f"#{waiting} waits on #{blocker}, which closed ({dependency.state_reason or 'no reason'}) "
            f"without delivering: its Done when still fails on main, so #{waiting} does not start. "
            f"Decide what #{waiting} waits on now (another issue, or no blocker)."),
            {"issue": waiting, "blocker": blocker})
    return passed


def _run(git, folder: Path, number: int, request: dict) -> bool:
    from foragent import capacity, run_checks

    checkout = folder / f"{number}-main"
    log = folder / f"{number}.log"
    log.write_text(f"Done when of #{number} on main {request['main']}\n", encoding="utf-8")
    if checkout.exists():
        git.remove_worktree(checkout)
    git.add_detached_worktree(checkout, request["main"])
    try:
        for command in request["commands"]:
            check = run_checks.check(command, checkout, log, PROOF_SECONDS, slot=capacity.hold_test_slot())
            if check.exit_code != 0 or check.timed_out:
                return False
        return True
    finally:
        git.remove_worktree(checkout)


def main(argv: list[str]) -> int:
    """Worker: the only writer of ``blocker-proofs/<N>.json``."""
    from foragent.git import GitRepo

    root, git_root, number = Path(argv[0]), Path(argv[1]), int(argv[2])
    folder = _folder(root)
    with open(folder / f"{number}.lock", "a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0  # Another worker proves this blocker.
        request = json.loads((folder / f"{number}.request.json").read_text(encoding="utf-8"))
        passed = _run(GitRepo(git_root), folder, number, request)
        target = folder / f"{number}.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps({"key": request["key"], "passed": passed}), encoding="utf-8")
        temporary.replace(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
