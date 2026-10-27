"""`fa orch stop`: the durable stop request and the fallback owner's steps (D95).

While its process lives, the runner alone owns the run record, its worktree
and its finish. The stop command writes a request, terminates the runner's
tree through ``scheduler._kill_run(outcome="stopped")`` and acts only after
the whole tree is dead and ``worktree_lock(N)`` is free. Crash recovery is the
same fallback owner: it finishes a request whose runner tree and stop command
are both dead. Every step is idempotent, and deleting the request is always
the last one, because the request is the evidence recovery uses.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timedelta
import fcntl
import json
from pathlib import Path
import threading
from typing import TYPE_CHECKING, Any, Iterator

from foragent import processes
from foragent.git import GitError, GitRepo
from foragent.run_attempt import ExecutorStopped
from foragent.state import Run

if TYPE_CHECKING:
    from foragent.scheduler import TickContext, TickReport

STOPPED = "stopped"
TREE_WAIT_S = 60  # The stop command waits this long for the runner's tree to die.
LOCK_WAIT_S = 300  # ...and this long for worktree_lock(N).
STALE_REQUEST_S = 600  # A request for a finished or missing run expires after this.


def _path(store, number: int) -> Path:
    return store.root / "stops" / f"{number}.json"


def read_request(store, number: int) -> dict[str, Any] | None:
    try:
        return json.loads(_path(store, number).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def _write(store, record: dict[str, Any]) -> None:
    path = _path(store, record["number"])
    path.parent.mkdir(parents=True, exist_ok=True)
    store._atomic_write(path, json.dumps(record, ensure_ascii=False))


@contextmanager
def claim_lock(store, number: int, *, blocking: bool = True) -> Iterator[bool]:
    """Serialize reading, judging and replacing issue N's request; yield whether it is held.

    A stop command takes it to claim the request; a tick takes it (without waiting)
    for the whole time it acts on a request, so a claim never interleaves with it.
    """
    path = store.root / "stops" / f"{number}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    key, held = str(path.resolve()), _CLAIMS.__dict__.setdefault("paths", set())
    if key in held:  # Reentrant on this thread: a tick decision that calls _kill_run keeps its claim.
        yield True
        return
    with path.open("a", encoding="utf-8") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        held.add(key)
        try:
            yield True
        finally:
            held.discard(key)
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


_CLAIMS = threading.local()


@contextmanager
def tick_owns(store, number: int) -> Iterator[bool]:
    """For a tick's decision that writes run N (a kill, a crash finish): yield whether it may.

    It holds N's claim lock for the whole decision, so a stop command cannot claim
    N between the check and the write; with a request for the current run, it may not.
    """
    with claim_lock(store, number, blocking=False) as claimed:
        yield claimed and stop_request(store, number) is None


# What a takeover keeps: what the save made, and every process an earlier scan found.
CHECKPOINTS = ("saved", "seen")


def write_request(store, run: Run, why: str, now: datetime,
                  previous: dict[str, Any] | None = None) -> dict[str, Any]:
    """Record the stop before any signal: the stop command's own identity owns it while it lives.

    Called under claim_lock. A takeover of the same run keeps the earlier save checkpoints.
    """
    pid = processes.os.getpid()
    row = next((row for row in processes.list_processes() if row.pid == pid), None)
    record = {"number": run.number, "started_at": run.started_at, "why": why,
              "requested_at": now.isoformat(), "pid": pid, "start_time": row.start_time if row else None}
    if previous is not None and previous.get("started_at") == run.started_at:
        record.update({key: previous[key] for key in CHECKPOINTS if key in previous})
    _write(store, record)
    return record


def _same_claim(left: dict[str, Any] | None, right: dict[str, Any]) -> bool:
    return left is not None and all(left.get(key) == right.get(key)
                                    for key in ("started_at", "requested_at", "pid", "start_time"))


def drop_request(store, number: int, owner: dict[str, Any] | None = None) -> None:
    """Delete the request; with `owner`, only while it is still that claim (never a newer one)."""
    if owner is None or _same_claim(read_request(store, number), owner):
        _path(store, number).unlink(missing_ok=True)


def stop_request(store, number: int) -> dict[str, Any] | None:
    """The request for issue N's current run; one left for an older run is not this run's."""
    record = read_request(store, number)
    run = store.get_run(number)
    return record if record is not None and run is not None and record.get("started_at") == run.started_at else None


class StopRequested(ExecutorStopped):
    """A stop request exists: the runner exits without writing its record, labels or WIP."""

    def __init__(self) -> None:
        super().__init__(STOPPED, "A stop was requested; fa orch stop finishes this run.")


def stop_owns(store, number: int) -> bool:
    """A runner finalizer's check: with a request, the stop command is the run's only writer.

    A merge already made is the runner's to finish; the stop then changes nothing.
    """
    run = store.get_run(number)
    return stop_request(store, number) is not None and not runner_merged(store, run)


def leave_to_stop(store, number: int) -> None:
    if stop_owns(store, number):
        raise StopRequested()


def yield_to_stop(store, number: int) -> None:
    """In the run's own runner process only, refuse to write once a stop owns the run.

    actions.flag calls this first, so no runner refusal (label, comment, flag
    record) lands after a request; the gate and the stop command are unaffected.
    """
    run = store.get_run(number)
    if run is not None and run.status == "running" and run.pid == processes.os.getpid():
        leave_to_stop(store, number)


class StopGuard:
    """The runner's tracker: once a stop request owns the run, no GitHub write is made."""

    WRITES = frozenset({"add_labels", "remove_labels", "comment", "close_issue", "create_pr", "update_pr",
                        "close_pr", "merge_pr", "create_issue", "update_issue", "queue_write"})

    def __init__(self, tracker, store, number: int) -> None:
        self._tracker, self._store, self._number = tracker, store, number

    def __getattr__(self, name: str):
        value = getattr(self._tracker, name)
        if name not in self.WRITES:
            return value

        def guarded(*args, **kwargs):
            leave_to_stop(self._store, self._number)
            return value(*args, **kwargs)

        return guarded


@contextmanager
def guard_runner(ctx, number: int) -> Iterator[None]:
    """While the runner runs, its tracker's writes check the stop request first.

    The caller's context keeps its identity; its tracker is restored on exit.
    """
    original = ctx.tracker
    ctx.tracker = StopGuard(original, ctx.store, number)
    try:
        yield
    finally:
        ctx.tracker = original


def command_alive(record: dict[str, Any]) -> bool:
    """Whether the stop command that wrote the request still lives; unknown counts as alive."""
    try:
        table = processes.list_processes()
    except PermissionError:
        return True
    pid, start = record.get("pid"), record.get("start_time")
    if pid in getattr(table, "unreadable", frozenset()):
        return True
    return any(row.pid == pid and (start is None or row.start_time == start) for row in table)


def tree_alive(run: Run, seen: dict[int, float] | None = None) -> bool:
    """Whether any process of the run's tree may live, from a fresh process scan.

    The tree is the recorded runner PID, executor group and process identities,
    every member of the runner's and the executor's process groups (whether or
    not the group leader still lives), every descendant the scan finds, and
    every process an earlier scan passed in `seen` found (so a descendant that
    was reparented after its parent died still counts). A recorded identity
    counts only with its start time (a reused PID is another process); a PID
    recorded without an identity counts by number. An unreadable recorded or
    seen PID of this user is unknown, which is never proof of death.
    """
    table = processes.list_processes()
    rows = {row.pid: row for row in table}
    unreadable = getattr(table, "unreadable", frozenset())
    owners, own = getattr(table, "unreadable_uids", {}), processes.os.getuid()
    known = {**(seen or {}), **{item["pid"]: item["start_time"] for item in run.process_identities}}
    if any(pid in unreadable and owners.get(pid, own) == own for pid in known):
        return True
    alive = {pid for pid, start in known.items() if pid in rows and rows[pid].start_time == start}
    alive |= {pid for pid in (run.pid, run.executor_pgid) if pid is not None and pid not in known and pid in rows}
    groups = {pid for pid in (run.pid, run.executor_pgid) if pid is not None}
    alive |= {row.pid for row in table if row.pgid in groups}
    grew = True
    while grew:
        found = {row.pid for row in table if row.ppid in alive} - alive
        alive |= found
        grew = bool(found)
    if seen is not None:
        seen.update({pid: rows[pid].start_time for pid in alive})
    return bool(alive)


def _attempts(ctx, seconds: int) -> Iterator[None]:
    """Yield before each attempt, sleeping 1 s between them, until `seconds` pass on ctx.now().

    The deadline is elapsed time on the injectable clock. The attempt count is
    only a backstop, so a clock that never moves (a test's) still ends.
    """
    deadline = ctx.now() + timedelta(seconds=seconds)
    for _ in range(seconds + 1):
        yield
        if ctx.now() >= deadline:
            return
        ctx.sleep(1)


def _poll(ctx, seconds: int, done: Callable[[], bool]) -> bool:
    return any(done() for _ in _attempts(ctx, seconds))


def scan_tree(store, run: Run, record: dict[str, Any]) -> bool:
    """tree_alive with the request's durable list of processes found by any earlier scan.

    New finds are written to the request before this returns, so a later poll,
    a takeover or the tick still counts a descendant reparented meanwhile.
    """
    seen = {int(pid): start for pid, start in record.get("seen", {}).items()}
    alive = tree_alive(run, seen)
    found = {str(pid): start for pid, start in seen.items()}
    if found != record.get("seen", {}) and _same_claim(read_request(store, run.number), record):
        record["seen"] = found
        _write(store, record)
    return alive


def wait_tree_dead(ctx, run: Run, record: dict[str, Any]) -> bool:
    """Poll until no process of the tree lives, for up to TREE_WAIT_S on the clock."""
    def dead() -> bool:
        try:
            return not scan_tree(ctx.store, run, record)
        except PermissionError:
            return False  # An unreadable table proves nothing.

    return _poll(ctx, TREE_WAIT_S, dead)


def runner_alive(run: Run) -> bool:
    """Whether the recorded runner process itself (not just some of its tree) still lives."""
    table = processes.list_processes()
    start = {item["pid"]: item["start_time"] for item in run.process_identities}.get(run.pid)
    if run.pid in getattr(table, "unreadable", frozenset()):
        return True
    return any(row.pid == run.pid and (start is None or row.start_time == start) for row in table)


def wait_merge(ctx, run: Run) -> bool:
    """Wait (up to LOCK_WAIT_S) for a merge call the live runner already made: it completes."""
    from foragent.orch_stop_checkpoint import merge_in_flight

    return _poll(ctx, LOCK_WAIT_S, lambda: not merge_in_flight(ctx.store, run.number) or not runner_alive(run))


@contextmanager
def worktree_lock_within(ctx, number: int, seconds: int = LOCK_WAIT_S) -> Iterator[bool]:
    """Non-blocking attempts on worktree_lock(N) until `seconds` pass; yield whether it is held."""
    for _ in _attempts(ctx, seconds):
        with ctx.store.try_worktree_lock(number) as held:
            if held:
                yield True
                return
    yield False


class StopStepFailed(Exception):
    """A step after the kill failed; the request stays and the next tick repeats the steps."""

    def __init__(self, step: str, detail: str) -> None:
        super().__init__(f"step {step} failed: {detail}")
        self.step = step


def _summary(number: int, why: str, saved: str | None) -> str:
    kept = ("nothing to save" if saved in (None, "nothing")
            else f"kept unfinished work at {saved[:12]} on foragent/issue-{number}")
    return f"#{number}: stopped; {kept}; why: {why}"


def runner_merged(store, run: Run | None) -> bool:
    """This run merged its PR (recorded beside the run record) before its tree died.

    A merge time from an earlier run of the issue is not this run's merge.
    """
    if run is None:
        return False
    if run.outcome == "merged":
        return True
    try:
        merged = json.loads((store.root / "runs" / f"{run.number}.json").read_text(encoding="utf-8")).get("merged_at")
        return merged is not None and datetime.fromisoformat(merged) >= datetime.fromisoformat(run.started_at)
    except (OSError, ValueError, TypeError):
        return False


def finish(ctx: TickContext, number: int, record: dict[str, Any]) -> str:
    """Finish a stop after the tree is dead, holding worktree_lock(N); returns the summary.

    Each step is idempotent, so a failed step (StopStepFailed) is repeated by the
    next attempt. A run the runner merged, or a closed issue, changes nothing
    beyond the request's removal.
    """
    from foragent import scheduler

    run = ctx.store.get_run(number)
    if run is None or run.started_at != record["started_at"]:
        drop_request(ctx.store, number, record)
        return f"#{number}: its run record is gone; the stop request was removed"
    if runner_merged(ctx.store, run):
        drop_request(ctx.store, number, record)
        return f"#{number}: runner merged its PR before the stop; nothing else changed"
    if run.status == "finished" and run.outcome != STOPPED:
        drop_request(ctx.store, number, record)
        raise StopStepFailed("check", f"the run already finished as {run.outcome}; nothing was stopped")
    try:
        issue = ctx.tracker.get_issue(number)
    except Exception as exc:
        raise StopStepFailed("read-issue", str(exc)) from exc
    if issue.state != "open":
        drop_request(ctx.store, number, record)
        return f"#{number}: the issue is closed; nothing else changed"
    why = record["why"]
    saved = _save(ctx, run, dict(record), issue.title)
    summary = _summary(number, why, saved)
    if _journal_once(ctx, run, summary):
        try:  # The local journal is the record; the GitHub comment is best effort.
            ctx.tracker.comment(number, "<!-- foragent:orch -->\n" + summary)
        except Exception:
            pass
    if run.status == "running":
        ctx.store.finish_run(number, outcome=STOPPED, detail=f"{STOPPED}: {why}")
    try:
        if (pr := ctx.tracker.find_pr(f"foragent/issue-{number}")) is not None:
            ctx.tracker.close_pr(pr, "Closed by Foragent orchestrator stop; the next run resumes from the "
                                     f"issue branch. Why: {why}")
            summary += f"; closed PR #{pr}"
        ctx.tracker.add_labels(number, [scheduler.READY])
        for label in (scheduler.BLOCKED, scheduler.NEEDS_HUMAN, scheduler.RUNNING):
            ctx.tracker.remove_labels(number, [label])  # One per call: a failure leaves the rest to the retry.
        scheduler.board.sync_status(ctx, number, "Ready")
    except Exception as exc:
        raise StopStepFailed("labels", " ".join(str(exc).split()) or type(exc).__name__) from exc
    drop_request(ctx.store, number, record)  # Last: the request is the evidence a retry uses.
    return summary


def _save(ctx: TickContext, run: Run, record: dict[str, Any], title: str) -> str | None:
    """Commit the dead tree's worktree as a WIP naming the why, publish it and remove the worktree.

    A checkpoint left by a step the kill cut is the run's work, not the half-built
    worktree: it is restored first. Saved means the branch holds work origin does
    not have (this WIP or the run's own earlier commits); only then is it pushed.
    """
    from foragent import orch_stop_checkpoint
    from foragent.run_publish import _preserve_failed_implementation

    number, worktree = run.number, Path(run.worktree)
    branch = f"foragent/issue-{number}"
    state = orch_stop_checkpoint.read_checkpoint(ctx.store, number)
    if not worktree.exists() and state is None:
        return record.get("saved")
    try:
        if state is not None:
            orch_stop_checkpoint.restore(ctx, worktree, state)
            orch_stop_checkpoint.drop_checkpoint(ctx.store, number)  # The worktree holds that work now.
        git = ctx.git.at(worktree)
        status = _preserve_failed_implementation(
            number, ctx, git, f"{STOPPED}: {record['why']}", approval=ctx.store.load_approval(number),
            title=title, tracker_available=False, stop=True,
        )
        if status is None:
            raise GitError(f"{worktree} is not a verifiable Git worktree")
        head = git.head_sha()
        published = ctx.git.remote_branch_sha(branch) or f"origin/{run.files_base_branch or ctx.config.base_branch}"
        ahead = int(git._call(["rev-list", "--count", "HEAD", "--not", published]).strip() or 0)
    except Exception as exc:
        raise StopStepFailed("save", f"the worktree {worktree} is kept: {exc}") from exc
    record["saved"] = head if ahead else "nothing"
    _write(ctx.store, record)
    if ahead:
        try:
            git._call(["push", "origin", f"HEAD:refs/heads/{branch}"])  # Normal, never forced.
        except GitError as exc:
            raise StopStepFailed("push", f"WIP {head} is kept locally on {branch}; "
                                         f"push rejected: {' '.join(str(exc).split())}") from exc
    try:
        ctx.git.remove_worktree(worktree)
    except GitError as exc:
        raise StopStepFailed("remove-worktree", str(exc)) from exc
    return record["saved"]


def _journal_once(ctx: TickContext, run: Run, summary: str) -> bool:
    """Append this run's one stop event; False if an earlier attempt already did."""
    from foragent import scheduler

    journal = ctx.store.root / "orch-events.jsonl"
    started = datetime.fromisoformat(run.started_at)
    if journal.exists() and any(
        event.get("issue") == run.number and event.get("kind") == "stop"
        and datetime.fromisoformat(event["at"]) >= started
        for event in map(json.loads, journal.read_text(encoding="utf-8").splitlines())
    ):
        return False
    scheduler.append_event(ctx.store, ctx.now(), run.number, "stop", summary)
    return True


def recover_requests(ctx: TickContext, report: TickReport) -> set[int]:
    """The tick's part: finish, expire or leave each request; returns the issues to leave alone.

    While a request's stop command lives, it owns the stop. A dead command with a
    live runner means the stop died before its kill: the request expires and
    normal recovery applies again. A dead command and a dead runner make this tick
    the fallback owner, which ends what is left of the tree and finishes the stop
    as the command would. Each
    request is read and acted on under its claim lock, so a stop command that
    claims it meanwhile is never overruled.
    """
    directory = ctx.store.root / "stops"
    skipped: set[int] = set()
    for path in sorted(directory.glob("*.json")) if directory.exists() else ():
        number = int(path.stem)
        with claim_lock(ctx.store, number, blocking=False) as claimed:
            record = read_request(ctx.store, number) if claimed else None
            if not claimed or record is None:
                if claimed is False:
                    skipped.add(number)  # A stop command is claiming it right now.
                continue
            if not _recover_one(ctx, report, record):
                skipped.add(number)
    return skipped


def _recover_one(ctx: TickContext, report: TickReport, record: dict[str, Any]) -> bool:
    """Act on one request under its claim lock; True when normal recovery may handle the issue again.

    The stop died before its kill only if the runner itself still lives. A dead
    runner with descendants left is this tick's stop to finish: it terminates
    them (the stop path's kill) and finishes once the whole recorded tree is gone.
    """
    from foragent import scheduler

    number = record["number"]
    run = ctx.store.get_run(number)
    current = run is not None and run.started_at == record["started_at"]
    if command_alive(record):
        return False
    if current and (run.status == "running" or run.outcome == STOPPED):
        try:
            if run.status == "running" and runner_alive(run):
                _expire(ctx, report, record)  # The stop died before its kill.
                return True
            alive = scan_tree(ctx.store, run, record)
            if alive and run.status == "running":
                scheduler._kill_run(ctx, number, report, outcome=STOPPED)
                alive = scan_tree(ctx.store, run, record)
        except PermissionError:
            return False  # Unknown liveness: neither expire nor finish.
        if alive:
            return False  # The next tick looks again; a finished run's leftovers go to the reaper.
    elif ctx.now() - datetime.fromisoformat(record["requested_at"]) >= timedelta(seconds=STALE_REQUEST_S):
        _expire(ctx, report, record)
        return True
    else:
        return not current
    with ctx.store.try_worktree_lock(number) as held:
        if held:
            try:
                report.actions.append(f"#{number} finished a stop: {finish(ctx, number, record)}")
            except StopStepFailed as exc:
                report.actions.append(f"#{number} stop not finished: {exc}")
    return False


def _expire(ctx: TickContext, report: TickReport, record: dict[str, Any]) -> None:
    from foragent import scheduler

    number = record["number"]
    drop_request(ctx.store, number, record)
    summary = f"Removed a stale stop request for #{number}"
    scheduler.append_event(ctx.store, ctx.now(), number, "stop-request-expired", summary)
    report.actions.append(f"#{number} {summary}")


def _merge_unless_stopped(ctx, n: int, merge: Callable[[], None]) -> None:
    """Merge only without a stop request, rechecked under worktree_lock(N) right before the call.

    Once called, the merge completes and is recorded in the same section, so a
    stop that follows sees the merge and changes nothing.
    """
    from foragent.orch_stop_checkpoint import merging

    with ctx.store.worktree_lock(n):
        leave_to_stop(ctx.store, n)
        with merging(ctx.store, n):  # A stop arriving now waits for this call instead of cutting it.
            merge()
            ctx.store.record_merge(n, ctx.now())


def _attach_issue_branch(git: GitRepo, branch: str) -> None:
    """Put a stop's WIP on the issue branch, which the next run starts from.

    A review-only or resolve worktree sits on a detached PR head. The branch is
    moved to it only when that keeps every branch commit (a fast-forward);
    otherwise nothing is committed and the save fails, keeping the worktree.
    """
    try:
        if git._call(["symbolic-ref", "-q", "HEAD"]).strip() == f"refs/heads/{branch}":
            return
    except GitError:
        pass  # Detached HEAD.
    if (tip := git.local_branch_sha(branch)) is not None:
        try:
            git._call(["merge-base", "--is-ancestor", tip, "HEAD"])
        except GitError as exc:
            raise GitError(f"the worktree's HEAD does not contain {branch}; nothing is committed") from exc
    git.attach_branch(branch)


def observe_live_changes(ctx, n: int, worktree: Path) -> None:
    """Record the paths an implementer turn changed, as provenance for a later WIP (#572).

    Taken when the turn ends, never during cleanup, so a deletion a racing
    removal causes later stays unowned. Best effort: without it, deletions are
    refused as before.
    """
    run = ctx.store.get_run(n)
    if run is None or run.status != "running" or not (worktree / ".git").exists():
        return
    try:
        git = ctx.git.at(worktree)
        base = git._own_base(f"origin/{run.files_base_branch or ctx.config.base_branch}")
        output = git._call(["diff", "--cached", "--name-only", "--no-renames", "-z", base])
        output += git._call(["diff", "--name-only", "--no-renames", "-z", base])
    except (GitError, ValueError):
        return
    paths = sorted(set(filter(None, output.split("\0"))) | set(run.live_changes))
    if paths != run.live_changes:
        ctx.store.update_run(n, live_changes=paths)
