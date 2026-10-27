---
name: project-415-sleep-ledger
description: #415 machine sleep credit — final single-writer design after the orchestrator narrowed the issue (2026-10-27); why ten ledger rounds failed
metadata:
  type: project
---

Final design (2026-10-27, issue narrowed after four review holds; wake grace moved to #650):
- `capacity.slept_seconds(since)` is the sensor: Darwin = max(kernel sleep/wake pair ∩ [since, now], mach_continuous−mach_absolute growth); elsewhere BOOTTIME−MONOTONIC or wall−monotonic vs a per-point baseline in the machine-wide `sleep-baselines.json`, pruned by age (48 h) only.
- `sleep_credit.credit(store, now)` is the only reader and writer: called at the top of `scheduler_recovery._recover_runs` (full tick only), under the state lock, from `<state>/sleep-baseline.json`'s previous tick time. The increment is clamped to 0..wall time and added to `Run.slept_seconds` and `Run.slept_since_progress` of non-waiting runs (phase ending `-wait` or future `waiting_until`) that started at or before the previous tick. Progress newer than the previous tick resets `slept_since_progress` first.
- Runtime limit = elapsed − waited − slept_seconds; stall = since progress − slept_since_progress; `run_waits._remaining` reads the run record only. `_test_slot`, `run_execution`, `state_runs` are main's versions.

Lessons:
- Ten rounds of ledger/placement/marker patches each produced new races. The fix was a single writer (D95 state-ownership rule: one writer per state, derive, append), not more cases. After two holds in one area, redesign.
- `state_runs.py` is at 298/300 lines (`test_state_split`); new state logic goes in its own module.
- `tests/conftest.py` stubs `capacity.slept_seconds` to 0 except in `test_machine_sleep`/`test_sleep_credit`.
- Python `time.monotonic` is `mach_absolute_time()` on macOS (pauses in sleep).
- During a runner-driven merge, resolve conflicts in the working tree and leave the index to the runner (undo any `git rm --cached` with `git reset -- <path>`).
