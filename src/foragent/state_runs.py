"""Run records, run-start journal, pauses and run history."""

from datetime import datetime
from pathlib import Path
from typing import Any

from foragent.state_phases import close_phase, work_event  # noqa: F401
from foragent.state_records import Run


# Updates passing any of these change or attest process ownership, so they
# observe the process tree; every other update is metadata only.
PROCESS_FIELDS = frozenset({"process_identities", "pid", "executor_pgid"})


class RunRecordsMixin:
    def _close_phase(self, run: Run, closed_at: datetime, next_step: str) -> dict[str, Any]:
        """Journal the ended phase; called under the state lock (see state_phases.close_phase)."""
        return close_phase(self.root, run, closed_at, next_step)

    def start_run(
        self, number: int, *, branch: str, worktree: str | Path, pid: int | None = None,
        lane: str = "main", tier: str = "m", implementer: str | None = None, journal: Any = None,
        executor: str | None = None, reviewer: str | None = None,
    ) -> Run:
        """Create a run; pid attests self or a newly spawned, unreaped child."""
        with self.worktree_lock(number), self._state_lock():  # Log pruning takes it too; callers must not hold it.
            timestamp = self._timestamp()
            approval = self.load_approval(number)
            previous = self.get_run(number)
            # A gate restart (stall, crash) or limit-wait resume continues the same attempt (#238):
            # its number, restarts and tier state survive, so a turn never reuses an earlier one's name.
            restarted = previous is not None and previous.outcome in {"stalled-restart", "crashed", "limit-wait"}
            restarts = previous.restarts if restarted else 0
            attempts = previous.attempts if restarted else 1
            session = {}
            if previous is not None:
                reason = "\n".join(value for value in (
                    previous.outcome, previous.detail, previous.resume_reason,
                ) if value)
                session = {
                    "session_id": previous.session_id, "session_executor": previous.session_executor,
                    "session_ids": previous.session_ids, "session_body": previous.session_body,
                    "merge_conflicts": previous.merge_conflicts, "work_events": previous.work_events,
                    "resume_whole": previous.resume_whole, "moved_files": previous.moved_files,
                    "local_skill_paths": previous.local_skill_paths,
                    "resume_reason": reason + (
                        "\nThe runner recreated the worktree from its target branch; "
                        "earlier uncommitted changes are no longer present."
                    ),
                }
            from foragent.processes import observe_tree

            identities = observe_tree(roots=(pid,)) if pid is not None else []
            run = _state.Run(number, branch, str(worktree), pid, timestamp, timestamp, attempts=attempts,
                      restarts=restarts, lane=lane, tier=previous.tier if restarted else tier,
                      attempt_tiers=previous.attempt_tiers if restarted else {},
                      open_turn=previous.open_turn if restarted else None,
                      approval_body_sha=approval.body_sha if approval is not None else None,
                      last_activity_at=timestamp, process_identities=identities, implementer=implementer,
                      executor=executor, reviewer=reviewer, **session)
            self._merge_lock_seconds.pop((number, timestamp), None)
            self._write_run(run)
            if journal is not None:  # The audit event of this record (#107), under the same lock.
                journal()
            return run

    def prepare_run_start(self, number: int) -> None:
        """Journal the label transition before a runner or run record exists."""
        self._write_json(self.root / "run-starts" / f"{number}.json", {
            "number": number, "prepared_at": self._timestamp(),
        })

    def run_start_pending(self, number: int) -> bool:
        return (self.root / "run-starts" / f"{number}.json").exists()

    def run_starts(self) -> dict[int, str]:
        """Map each journaled, not yet cleared start to its prepared_at time."""
        starts = {}
        for path in (self.root / "run-starts").glob("*.json"):
            value = _state.json.loads(path.read_text(encoding="utf-8"))
            starts[int(value["number"])] = value["prepared_at"]
        return starts

    def clear_run_start(self, number: int) -> None:
        (self.root / "run-starts" / f"{number}.json").unlink(missing_ok=True)

    def update_run(
        self, number: int, *, routine: bool = False, observe: bool = False, **fields: Any,
    ) -> Run:
        """Save ``fields``; observe the process tree only when asked or ownership changes.

        Metadata-only updates keep the saved identities as they are; the tick's
        process pass observes and prunes them once per tick.
        """
        observe = observe or not PROCESS_FIELDS.isdisjoint(fields)
        with self._state_lock():
            run = self.get_run(number)
            if run is None:
                raise KeyError(number)
            if run.status == "finished":
                return run
            if "detail" in fields:
                fields["detail"] = (
                    " ".join(fields["detail"].split())[:500]
                    or " ".join(run.detail.split())[:500]
                    or "Run detail unavailable."
                )
            if "phase" in fields and fields["phase"] != run.phase:
                closed = fields.get("phase_started_at")
                changes = self._close_phase(run, _state.datetime.fromisoformat(closed) if closed else self._now(),
                                            fields["phase"])
                # A new phase has no work identity unless the caller restores or gives one.
                fields = {**changes, "phase_work_id": None, "phase_work_started_at": None, **fields}
                if fields["phase_work_id"] and not fields["phase_work_started_at"]:
                    # Work named with its phase begins with that phase; nested waits keep this instance.
                    fields["phase_work_started_at"] = fields.get("phase_started_at") or self._timestamp()
            elif fields.get("phase_work_id") and "phase_work_started_at" not in fields:
                fields["phase_work_started_at"] = run.phase_started_at
            if not observe:
                updated = _state.replace(run, **fields)
                self._write_run(updated)
                return updated
            from foragent.processes import observe_routine_tree, observe_tree

            # Numeric fields describe a run; only birth evidence supplied by a
            # launcher (or descendants of verified identities) proves ownership.
            # An explicit new birth may reuse an earlier executor's PID.
            identities = {item["pid"]: item for item in (
                *run.process_identities, *fields.get("process_identities", []),
            )}
            observer = observe_routine_tree if routine else observe_tree
            try:
                fields["process_identities"] = observer(list(identities.values()))
            except OSError:
                # Metadata updates must retain ownership even when observation fails.
                fields["process_identities"] = list(identities.values())
            updated = _state.replace(run, **fields)
            self._write_run(updated)
            return updated

    def get_run(self, number: int) -> Run | None:
        try:
            value = (self.root / "runs" / f"{number}.json").read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        return _state.load_record(_state.Run, _state.json.loads(value))

    def record_merge_lock(self, number: int, seconds: float) -> None:
        run = self.get_run(number)
        key = (number, run.started_at)
        total = self._merge_lock_seconds.get(key, 0) + seconds
        self._merge_lock_seconds[key] = total
        if run.merge_lock_held_seconds is not None:
            self.update_run(number, merge_lock_held_seconds=total)

    def record_merge(self, number: int, merged_at: datetime) -> None:
        run = self.get_run(number)
        passed = run.last_review_pass_at
        self.update_run(
            number, review_pass_to_merge_seconds=(
                (merged_at - _state.datetime.fromisoformat(passed)).total_seconds() if passed else None
            ), merge_lock_held_seconds=self._merge_lock_seconds.get((number, run.started_at), 0),
        )
        # The merge moment is saved beside the record's fields (the Run schema stays
        # as it was); finish_run carries it into history and merge_times() reads it.
        with self._state_lock():
            path = self.root / "runs" / f"{number}.json"
            value = _state.json.loads(path.read_text(encoding="utf-8"))
            value["merged_at"] = merged_at.astimezone(_state.UTC).isoformat()
            self._write_json(path, value)

    def active_runs(self) -> list[Run]:
        paths = list((self.root / "runs").glob("*.json"))  # Issues by number, then dream-<lane> records.
        runs = [*map(self.get_run, sorted(int(path.stem) for path in paths if path.stem.isdigit())),
                *(self.get_dream(path.stem[6:]) for path in sorted(paths) if path.stem.startswith("dream-"))]
        return [run for run in runs if run is not None and run.status == "running"]

    def pause_lane(self, lane: str, until: datetime) -> None:
        with self._state_lock():
            path = self.root / "lane-pauses.json"
            pauses = _state.json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            timestamp = until.astimezone(_state.UTC).isoformat()
            pauses[lane] = max(pauses.get(lane, timestamp), timestamp)
            self._write_json(path, pauses)

    def lane_paused(self, lane: str, now: datetime) -> bool:
        try:
            pauses = _state.json.loads((self.root / "lane-pauses.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return False
        return lane in pauses and now < _state.datetime.fromisoformat(pauses[lane])

    def pause_executor(self, executor: str, until: datetime) -> None:
        """Hold every lane that needs ``executor`` until its usage window resets."""
        with self._state_lock():
            path = self.root / "executor-pauses.json"
            pauses = _state.json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            timestamp = until.astimezone(_state.UTC).isoformat()
            current = pauses.get(executor)
            if current is None or _state.datetime.fromisoformat(current) < until:
                pauses[executor] = timestamp
            self._write_json(path, pauses)

    def executor_pause_until(self, executor: str, now: datetime) -> datetime | None:
        """When a usage-limit pause of ``executor`` ends, or None when it is not paused."""
        return self.executor_pauses(now).get(executor)

    def executor_paused(self, executor: str, now: datetime) -> bool:
        return self.executor_pause_until(executor, now) is not None

    def pause_github(self, until: datetime) -> datetime:
        """Share a reset deadline across runners and ticks without shortening it."""
        with self._state_lock():
            until = (self.github_wait_until(until) or until).astimezone(_state.UTC)
            self._write_json(self.root / "github-pause.json", {"until": until.isoformat()})
            return until

    def github_wait_until(self, now: datetime) -> datetime | None:
        try:
            value = (self.root / "github-pause.json").read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        until = _state.datetime.fromisoformat(_state.json.loads(value)["until"])
        return until if now < until else None

    def finish_run(self, number: int | str, outcome: str, detail: str = "") -> Run:
        if isinstance(number, str):  # A dream record (dream-<lane>) never enters runs.jsonl.
            return self.finish_dream(number[6:], outcome, detail)
        with self._state_lock():
            run = self.get_run(number)
            if run is None:
                raise KeyError(number)
            if run.status == "finished":
                return run
            detail = " ".join(detail.split())[:500]
            if not detail and outcome != "merged":
                detail = " ".join(run.detail.split())[:500] or f"Run finished with outcome {outcome}."
            from foragent.processes import observe_tree

            # Every run exit closes its last open phase segment.
            finished = _state.replace(run, **self._close_phase(run, self._now(), outcome))
            finished = _state.replace(finished, status="finished", outcome=outcome, detail=detail,
                                      process_identities=observe_tree(run.process_identities))
            history_path = self.root / "runs.jsonl"
            history = history_path.read_text(encoding="utf-8") if history_path.exists() else ""
            # The finish time is saved beside the record's fields, so the Run schema
            # stays as it was; finish_times() reads it back.
            snapshot = {**self._run_value(finished), "finished_at": self._timestamp()}
            self._atomic_write(history_path, history + _state.json.dumps(snapshot, ensure_ascii=False) + "\n")
            self._write_run(finished, snapshot)
            return finished

    def read_history(self) -> list[Run]:
        """Return each execution once, retaining its latest completion entry."""
        try:
            history = (self.root / "runs.jsonl").read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        runs: dict[tuple[int, str], _state.Run] = {}
        for line in history.splitlines():
            run = _state.load_record(_state.Run, _state.json.loads(line))
            runs[run.number, run.started_at] = run
        return list(runs.values())

    def finish_times(self) -> dict[tuple[int, str], str]:
        """When each finished execution ended, by (number, started_at), from its latest entry.

        Records written before finish times were saved have none."""
        return self._history_times("finished_at")

    def merge_times(self) -> dict[tuple[int, str], str]:
        """When the gate merged each execution's PR, by (number, started_at).

        A record keeps the key of an earlier execution of the same issue until its
        own merge replaces it, so a time before the execution's start is not its own."""
        return self._history_times("merged_at")

    def _history_times(self, key: str) -> dict[tuple[int, str], str]:
        try:
            history = (self.root / "runs.jsonl").read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        times: dict[tuple[int, str], str] = {}
        for line in history.splitlines():
            value = _state.json.loads(line)
            if isinstance(value.get(key), str):
                times[value["number"], value["started_at"]] = value[key]
        return times

    def prune_process_identities(self, *, inactive_only: bool = False) -> None:
        from foragent.state_processes import prune_process_identities

        prune_process_identities(self, inactive_only=inactive_only)


# Bound last: foragent.state imports this module; call-time lookups reach facade patches.
import foragent.state as _state
