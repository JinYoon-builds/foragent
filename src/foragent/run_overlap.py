"""Wait for late file claims and persist file-level overlap/conflict evidence."""

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from glob import escape

from foragent.overlap_files import Claim, any_overlap, approved_issue, at_capacity, claims, measure
from foragent.run_waits import _credited_wait


def _edit_records(ctx, number, edit) -> None:
    # Each runner may observe the same peer. Merge evidence under the state
    # lock so one observer cannot overwrite another's peers or conflict time.
    with ctx.store._state_lock():
        run = ctx.store.get_run(number)
        if run.status == "finished":
            return
        rows = {row["file"]: dict(row) for row in run.overlap_records}
        edit(rows)
        ctx.store._write_run(replace(run, overlap_records=[rows[path] for path in sorted(rows)]))


def _record_peers(ctx, owner: Claim, peers: list[Claim]) -> None:
    if owner.run.changed_files is None:
        return
    overlaps = {path: {peer.run.number for peer in peers if any_overlap(ctx, [escape(path)], peer.files)}
                for path in owner.run.changed_files}

    def edit(rows):
        for path, numbers in overlaps.items():
            if numbers:
                row = rows.setdefault(path, {"file": path, "issues": [], "conflicted": False,
                                             "resolution_seconds": 0, "redo_count": 0})
                row["issues"] = sorted(set(row["issues"]) | numbers)

    _edit_records(ctx, owner.run.number, edit)


def _late_blocked(ctx, owner: Claim, peers: list[Claim]) -> bool:
    own_order = (datetime.fromisoformat(owner.run.started_at), owner.run.number)
    # Stable holders keep their place. Expanding runs must use one total order
    # across all their claims, including declarations and exclusive reservations;
    # per-path precedence lets crossed additions wait on each other forever.
    holders = [peer for peer in peers if not peer.run.late_files
               or (datetime.fromisoformat(peer.run.started_at), peer.run.number) < own_order]
    for path in owner.run.late_files:
        pattern = escape(path)
        if any(any_overlap(ctx, [pattern], peer.parsed.exclusive) for peer in holders):
            return True
        if at_capacity(ctx, [pattern], holders):
            return True
    return False


def wait_for_overlap(attempt) -> None:
    ctx, n = attempt.ctx, attempt.n
    if not attempt.parsed.files:
        return

    def blocked():
        run = ctx.store.get_run(n)
        if approved_issue(ctx, run) is None:
            return False
        run = measure(ctx, run, attempt.parsed, attempt.target_branch)
        owner = Claim(run, attempt.parsed, [escape(path) for path in run.effective_files]
                      if run.files_committed and run.effective_files is not None else attempt.parsed.files)
        peers, _ = claims(ctx, exclude=n)
        _record_peers(ctx, owner, peers)
        for peer in peers:
            _record_peers(ctx, peer, [owner])
        return _late_blocked(ctx, owner, peers)

    if not blocked():
        return

    def acquire():
        while blocked():
            ctx.sleep(1)

    _credited_wait(n, ctx, acquire)


@contextmanager
def conflict_resolution(attempt, paths: list[str]):
    """One resolution interval, regardless of the number of conflicting peers."""
    ctx, n = attempt.ctx, attempt.n
    wait_for_overlap(attempt)
    started = ctx.now()
    waited = ctx.store.get_run(n).waited_seconds
    try:
        yield
    finally:
        seconds = max(0, (ctx.now() - started).total_seconds()
                      - (ctx.store.get_run(n).waited_seconds - waited))

        def edit(rows):
            for path in paths:
                if path in rows:
                    rows[path]["conflicted"] = True
                    rows[path]["resolution_seconds"] += seconds

        _edit_records(ctx, n, edit)


def record_redo(attempt) -> None:
    ctx, n = attempt.ctx, attempt.n
    paths = ctx.store.get_run(n).merge_conflicts

    def edit(rows):
        for path in paths:
            if path in rows:
                rows[path]["redo_count"] += 1

    _edit_records(ctx, n, edit)
