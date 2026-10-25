"""Prune recorded ownership only after fresh confirmation of cached absence."""

from dataclasses import replace
import json

from foragent import processes


def _retained_identities(identities, *, runner_pid: int | None = None):
    # Cached presence is positive evidence; absence is only a prune candidate.
    return [item for item in identities
            if item["pid"] == runner_pid
            or processes.recorded_identity_evidence(item["pid"], item["start_time"]).alive is not False]


def prune_process_identities(store, *, inactive_only: bool = False) -> None:
    snapshot = processes.current_snapshot()
    if snapshot is None or not snapshot.available:
        return
    with store._state_lock():
        for path in (store.root / "runs").glob("*.json"):
            run = store.get_run(int(path.stem))
            if run is None or (inactive_only and run.status == "running"):
                continue
            # Recovery needs the runner's birth identity even after death or PID reuse.
            identities = _retained_identities(
                run.process_identities, runner_pid=run.pid if run.status == "running" else None,
            )
            if identities != run.process_identities:
                store._write_run(replace(run, process_identities=identities))
        history_path = store.root / "runs.jsonl"
        if not history_path.exists():
            return
        records = [json.loads(line) for line in history_path.read_text(encoding="utf-8").splitlines()]
        changed = False
        for record in records:
            identities = record.get("process_identities", [])
            observed = _retained_identities(identities)
            if identities != observed:
                record["process_identities"] = observed
                changed = True
        if changed:
            store._atomic_write(history_path, "".join(json.dumps(record, ensure_ascii=False) + "\n"
                                                      for record in records))
