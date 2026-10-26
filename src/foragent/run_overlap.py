"""Wait for late file claims and persist file-level overlap/conflict evidence."""

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from glob import escape

from foragent.i18n import text
from foragent.overlap_files import Claim, any_overlap, approved_issue, at_capacity, claims, measure, union_patterns
from foragent.run_checks import _append
from foragent.run_waits import _credited_wait, _phase

# Seconds between re-checks while a late file waits for its holders. Each check
# measures this run and every peer, so a short poll multiplies tick load.
OVERLAP_POLL_SECONDS = 15


def _edited(run, edit) -> list[dict] | None:
    """The edited rows, or None when ``edit`` leaves them as they are."""
    rows = {row["file"]: deepcopy(row) for row in run.overlap_records}
    edit(rows)
    records = [rows[path] for path in sorted(rows)]
    return None if records == run.overlap_records else records


def _edit_records(ctx, number, edit) -> None:
    # Each runner may observe the same peer. Merge evidence under the state
    # lock so one observer cannot overwrite another's peers or conflict time.
    # Edits only add evidence, so rows that already hold it need neither the
    # lock nor a write.
    run = ctx.store.get_run(number)
    if run is None or run.status == "finished" or _edited(run, edit) is None:
        return
    with ctx.store._state_lock():
        run = ctx.store.get_run(number)
        if run.status == "finished":
            return
        records = _edited(run, edit)
        if records is not None:
            ctx.store._write_run(replace(run, overlap_records=records))


def _record_peers(ctx, owner: Claim, peers: list[Claim], *, union: set[str] | None = None) -> None:
    if owner.run.changed_files is None:
        return
    if union is None:
        union = union_patterns(ctx, [], [owner, *peers])
    overlaps = {path: {peer.run.number for peer in peers if any_overlap(ctx, [escape(path)], peer.files)}
                for path in owner.run.changed_files if escape(path) not in union}

    def edit(rows):
        for path, numbers in overlaps.items():
            if numbers:
                row = rows.setdefault(path, {"file": path, "issues": [], "conflicted": False,
                                             "resolution_seconds": 0, "redo_count": 0})
                row["issues"] = sorted(set(row["issues"]) | numbers)

    _edit_records(ctx, owner.run.number, edit)


def _late_blockers(ctx, owner: Claim, peers: list[Claim], union: set[str]) -> dict[str, list[int]]:
    own_order = (datetime.fromisoformat(owner.run.started_at), owner.run.number)
    # Stable holders keep their place. Expanding runs must use one total order
    # across all their claims, including declarations and exclusive reservations;
    # per-path precedence lets crossed additions wait on each other forever.
    holders = [peer for peer in peers if not peer.run.late_files
               or (datetime.fromisoformat(peer.run.started_at), peer.run.number) < own_order]
    blocked = {}
    for path in owner.run.late_files:
        pattern = escape(path)
        # Exclusive reservations, the owner's included, win over the union exemption.
        exclusive = [peer.run.number for peer in holders if any_overlap(ctx, [pattern], peer.parsed.exclusive)
                     or (any_overlap(ctx, [pattern], owner.parsed.exclusive) and any_overlap(ctx, [pattern], peer.files))]
        full = at_capacity(ctx, [pattern], holders, union=union)
        if exclusive or full:
            numbers = exclusive + ([peer.run.number for peer in holders
                                    if any_overlap(ctx, [pattern], peer.files)] if full else [])
            blocked[path] = sorted(set(numbers))
    return blocked


def wait_for_overlap(attempt) -> None:
    ctx, n = attempt.ctx, attempt.n
    if not attempt.parsed.files:
        return

    def blocked():
        run = ctx.store.get_run(n)
        if approved_issue(ctx, run) is None:
            return {}
        run = measure(ctx, run, attempt.parsed, attempt.target_branch)
        owner = Claim(run, attempt.parsed, [escape(path) for path in run.effective_files]
                      if run.files_committed and run.effective_files is not None else attempt.parsed.files)
        peers, _ = claims(ctx, exclude=n)
        union = union_patterns(ctx, [], [owner, *peers])
        _record_peers(ctx, owner, peers, union=union)
        for peer in peers:
            _record_peers(ctx, peer, [owner], union=union)
        return _late_blockers(ctx, owner, peers, union)

    blockers = blocked()
    if not blockers:
        return

    def acquire():
        while blocked():
            ctx.sleep(OVERLAP_POLL_SECONDS)

    previous_phase = ctx.store.get_run(n).phase
    _append(ctx.store.log_path(n), text(
        "overlap_wait_started", ctx.config.language,
        blockers="; ".join(f"{path}: " + ", ".join(f"#{number}" for number in numbers)
                           for path, numbers in sorted(blockers.items())),
    ))
    _phase(n, ctx, "overlap-wait")
    try:
        _credited_wait(n, ctx, acquire)
    finally:
        _phase(n, ctx, previous_phase)


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
