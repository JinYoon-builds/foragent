"""Finished run snapshots leave by the same outcome/age rule as their logs."""

from datetime import timedelta

from foragent import shelf_io
from foragent.state_runs import record_merged


def remove_with_logs(store, number, seen) -> None:
    """The caller holds the issue lock and has removed the selected whole log group."""
    with store._state_lock():
        try:
            run = store._read_issue_run(number)
        except FileNotFoundError:
            return
        if run.get("status") == "finished" and (seen is None or run["started_at"] == seen["started_at"]):
            shelf_io.remove(store.root / "runs" / f"{number}.json")


def prune_records(store, now) -> None:
    from foragent.state_logs import OTHER_RETENTION, _parse_time

    owner_facts = store.owner_merge_facts()
    for path in sorted((store.root / "runs").glob("*.json")):
        # Issue starts share the worktree lock with retention.
        # Existing retention expires issue logs only. Side records still carry draft
        # eligibility/dream state, and stay alongside their non-expiring side logs.
        if not path.stem.isdigit():
            continue
        lock = store.try_worktree_lock(int(path.stem))
        with lock as acquired:
            if not acquired:
                continue
            with store._state_lock():
                try:
                    run = store._read_issue_run(int(path.stem))
                except FileNotFoundError:
                    continue
                if run.get("status") != "finished":
                    continue
                if store.run_start_pending(int(path.stem)):
                    continue
                keep = timedelta(days=store._merged_retention_days()) if record_merged(run, owner_facts) else OTHER_RETENTION
                logs = store._issue_files(int(path.stem)) if (store.root / "logs").is_dir() else []
                at = _parse_time(run.get("finished_at")) or store._group_age(None, logs or [path])
                if now - at > keep:
                    shelf_io.remove(path)
