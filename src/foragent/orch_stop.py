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
import json
from pathlib import Path
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


def write_request(store, run: Run, why: str, now: datetime) -> dict[str, Any]:
    """Record the stop before any signal: the stop command's own identity owns it while it lives."""
    pid = processes.os.getpid()
    row = next((row for row in processes.list_processes() if row.pid == pid), None)
    record = {"number": run.number, "started_at": run.started_at, "why": why,
              "requested_at": now.isoformat(), "pid": pid, "start_time": row.start_time if row else None}
    _write(store, record)
    return record


def drop_request(store, number: int) -> None:
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


def tree_alive(run: Run) -> bool:
    """Whether any process of the run's tree may live, from a fresh process scan.

    The tree is the recorded runner PID, executor group and process identities
    plus every descendant the scan finds. A recorded identity counts only with
    its start time (a reused PID is another process); a PID recorded without
    an identity counts by number. An unreadable recorded PID of this user is
    unknown, which is never proof of death.
    """
    table = processes.list_processes()
    rows = {row.pid: row for row in table}
    unreadable = getattr(table, "unreadable", frozenset())
    owners, own = getattr(table, "unreadable_uids", {}), processes.os.getuid()
    identities = {item["pid"]: item["start_time"] for item in run.process_identities}
    if any(pid in unreadable and owners.get(pid, own) == own for pid in identities):
        return True
    alive = {pid for pid, start in identities.items() if pid in rows and rows[pid].start_time == start}
    alive |= {pid for pid in (run.pid, run.executor_pgid) if pid is not None and pid not in identities and pid in rows}
    if run.executor_pgid is not None and (run.executor_pgid in alive or run.executor_pgid not in identities):
        alive |= {row.pid for row in table if row.pgid == run.executor_pgid}
    grew = True
    while grew:
        found = {row.pid for row in table if row.ppid in alive} - alive
        alive |= found
        grew = bool(found)
    return bool(alive)


def _poll(ctx, seconds: int, done: Callable[[], bool]) -> bool:
    """Poll in 1 s steps of the injectable sleep and clock; at most `seconds` sleeps."""
    deadline = ctx.now() + timedelta(seconds=seconds)
    for _ in range(seconds):
        if done():
            return True
        if ctx.now() >= deadline:
            return False
        ctx.sleep(1)
    return done()


def wait_tree_dead(ctx, run: Run) -> bool:
    def dead() -> bool:
        try:
            return not tree_alive(run)
        except PermissionError:
            return False  # An unreadable table proves nothing.

    return _poll(ctx, TREE_WAIT_S, dead)


@contextmanager
def worktree_lock_within(ctx, number: int, seconds: int = LOCK_WAIT_S) -> Iterator[bool]:
    """Non-blocking attempts on worktree_lock(N) until `seconds` pass; yield whether it is held."""
    for attempt in range(seconds + 1):
        with ctx.store.try_worktree_lock(number) as held:
            if held:
                yield True
                return
        if attempt == seconds:
            break
        ctx.sleep(1)
    yield False


class StopStepFailed(Exception):
    """A step after the kill failed; the request stays and the next tick repeats the steps."""

    def __init__(self, step: str, detail: str) -> None:
        super().__init__(f"step {step} failed: {detail}")
        self.step = step


def _summary(number: int, why: str, saved: str | None) -> str:
    kept = ("nothing to save" if saved in (None, "nothing")
            else f"kept unfinished work as WIP {saved[:12]} on foragent/issue-{number}")
    return f"#{number}: stopped; {kept}; why: {why}"


def runner_merged(store, run: Run) -> bool:
    """The runner merged its PR (recorded beside the run record) before its tree died."""
    if run.outcome == "merged":
        return True
    try:
        return "merged_at" in json.loads((store.root / "runs" / f"{run.number}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
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
        drop_request(ctx.store, number)
        return f"#{number}: its run record is gone; the stop request was removed"
    if runner_merged(ctx.store, run):
        drop_request(ctx.store, number)
        return f"#{number}: runner merged its PR before the stop; nothing else changed"
    if run.status == "finished" and run.outcome != STOPPED:
        drop_request(ctx.store, number)
        raise StopStepFailed("check", f"the run already finished as {run.outcome}; nothing was stopped")
    try:
        issue = ctx.tracker.get_issue(number)
    except Exception as exc:
        raise StopStepFailed("read-issue", str(exc)) from exc
    if issue.state != "open":
        drop_request(ctx.store, number)
        return f"#{number}: the issue is closed; nothing else changed"
    why = record["why"]
    saved = _save(ctx, run, record, issue.title)
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
    drop_request(ctx.store, number)  # Last: the request is the evidence a retry uses.
    return summary


def _save(ctx: TickContext, run: Run, record: dict[str, Any], title: str) -> str | None:
    """Commit the dead tree's worktree as a WIP naming the why, push it and remove the worktree."""
    from foragent.run_publish import _preserve_failed_implementation

    number, worktree = run.number, Path(run.worktree)
    branch = f"foragent/issue-{number}"
    if worktree.exists():
        git = ctx.git.at(worktree)
        try:
            status = _preserve_failed_implementation(
                number, ctx, git, f"{STOPPED}: {record['why']}", approval=ctx.store.load_approval(number),
                title=title, tracker_available=False, stop=True,
            )
        except Exception as exc:
            raise StopStepFailed("save", f"the worktree {worktree} is kept: {exc}") from exc
        if status == "saved":
            record = {**record, "saved": git.head_sha()}
            _write(ctx.store, record)
        if record.get("saved") not in (None, "nothing"):
            try:
                git._call(["push", "origin", f"HEAD:refs/heads/{branch}"])  # Normal, never forced.
            except GitError as exc:
                raise StopStepFailed("push", f"WIP {record['saved']} is kept locally on {branch}; "
                                             f"push rejected: {' '.join(str(exc).split())}") from exc
        elif "saved" not in record:
            record = {**record, "saved": "nothing"}
            _write(ctx.store, record)
        try:
            ctx.git.remove_worktree(worktree)
        except GitError as exc:
            raise StopStepFailed("remove-worktree", str(exc)) from exc
    return record.get("saved")


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
    live runner tree means the stop died before its kill: the request expires and
    normal recovery applies again. A dead command and a dead tree make this tick
    the fallback owner, which finishes the stop as the command would.
    """
    from foragent import scheduler

    directory = ctx.store.root / "stops"
    skipped: set[int] = set()
    for path in sorted(directory.glob("*.json")) if directory.exists() else ():
        record = json.loads(path.read_text(encoding="utf-8"))
        number = record["number"]
        run = ctx.store.get_run(number)
        current = run is not None and run.started_at == record["started_at"]
        if command_alive(record):
            skipped.add(number)
            continue
        if current and run.status == "running":
            try:
                alive = tree_alive(run)
            except PermissionError:
                skipped.add(number)
                continue
            if alive:
                _expire(ctx, report, number)
                continue
        elif not current or run.outcome != STOPPED:
            if ctx.now() - datetime.fromisoformat(record["requested_at"]) >= timedelta(seconds=STALE_REQUEST_S):
                _expire(ctx, report, number)
            elif current:
                skipped.add(number)
            continue
        skipped.add(number)
        with ctx.store.try_worktree_lock(number) as held:
            if not held:
                continue
            try:
                report.actions.append(f"#{number} finished a stop: {finish(ctx, number, record)}")
            except StopStepFailed as exc:
                report.actions.append(f"#{number} stop not finished: {exc}")
    return skipped


def _expire(ctx: TickContext, report: TickReport, number: int) -> None:
    from foragent import scheduler

    drop_request(ctx.store, number)
    summary = f"Removed a stale stop request for #{number}"
    scheduler.append_event(ctx.store, ctx.now(), number, "stop-request-expired", summary)
    report.actions.append(f"#{number} {summary}")


def _merge_unless_stopped(ctx, n: int, merge: Callable[[], None]) -> None:
    """Merge only without a stop request, rechecked under worktree_lock(N) right before the call.

    Once called, the merge completes and is recorded in the same section, so a
    stop that follows sees the merge and changes nothing.
    """
    with ctx.store.worktree_lock(n):
        leave_to_stop(ctx.store, n)
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
