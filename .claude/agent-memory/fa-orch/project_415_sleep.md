---
name: project-415-sleep-ledger
description: #415 machine sleep credit — final design (append-only ledger of closed intervals, one locked snapshot per consumer) and why ten review rounds pushed there
metadata:
  type: project
---

Final design (2026-10-27, after three "held" reviews):
- `capacity.slept_seconds(since)` is only the sensor: Darwin kern.sleeptime/waketime ∩ [since, now]; elsewhere a BOOTTIME−MONOTONIC or wall−monotonic offset vs its baseline keyed by `since` (`sleep-baselines.json`, few keys).
- `sleep_ledger.read(now_fn, starts, tick=, report=, language=)` under its own lock appends `(since, until, seconds)` (previous reading → this reading) to `sleep-ledger.jsonl` and returns a Snapshot with direct sensor reads for `starts`. Credit = max(ledger lower bound, sensor). Prune only records ending at or before the oldest running start machine-wide; a reader that sees no running run prunes nothing (age-only retention was rejected).
- Consumers (`_remaining`, `_test_slot`, scheduler runtime and stall) credit `Snapshot.credit(start)` = overlap of [start, now]. An awake event (run start, progress, wait start) inside an interval proves that sleep ended before it. Wake grace opens only at gate ticks (`tick=True`) when sleep since the previous tick > 60 s, lasting 5 min from that tick; an event ends it only if < 1 s of wall time follows it in the latest grace record.
- Awake events are stamped by a reading (`sleep_ledger.mark(now)`: start_run, run_waits progress/waits, run_execution poll), so every event is a record boundary; following an event with a reading was rejected as non-atomic. Grace ends only for events at or after the grace record's end.
- The last record is the checkpoint (previous = max(state read_at, last record until)); appends close a torn last line first. Darwin sensor = max(kernel pair ∩ window, mach_continuous−mach_absolute growth); the measure bracket compares with abs() because the pair can go down.
- No version markers or legacy branches (the orchestrator rejected `sleep_free_waits`).

Lessons:
- When reviewers keep finding new edge cases in the same area, simplify the model instead of patching each one; ten rounds of patches (bisection placement, coarse bounds, event splits, markers) all produced new defects.
- `tests/conftest.py` replaces `sleep_ledger.read` with an empty snapshot except in `test_machine_sleep`/`test_sleep_ledger`/`test_sleep_judgment`: a reading writes the machine root (breaks `test_state.py`'s os.replace inspection) and the owner's real sleep would credit fake-clock runs.
- `state_runs.py` and scheduler split modules have 300-line limits (`test_state_split`, `test_scheduler_split`).
- Python `time.monotonic` is `mach_absolute_time()` on macOS (pauses in sleep), so monotonic-timed waits hold no sleep.
- Acceptance helpers are importable via `sys.path.insert(tests/acceptance)` (precedent `tests/test_orch_reviewers.py`). In zsh use arrays for file loops.
