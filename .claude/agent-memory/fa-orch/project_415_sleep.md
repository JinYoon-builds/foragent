---
name: project-415-sleep-ledger
description: #415 machine sleep credit — capacity.slept_seconds sensor, sleep_ledger.py machine-wide ledger, conftest stub, line-limit constraints
metadata:
  type: project
---

- Sensor `capacity.slept_seconds(since)` keeps a cumulative machine-wide interval history (`sleep-history.json`): every Darwin kern.sleeptime/waketime pair seen, plus offset growth (BOOTTIME−MONOTONIC or wall−monotonic) the pairs don't explain. Review rejected per-`since` baselines (evicted after N) and last-pair-only Darwin. Prune by age (30 d) and oldest running run machine-wide.
- Python `time.monotonic` is `mach_absolute_time()` on macOS (pauses in sleep), so monotonic-timed waits hold no sleep; only wall-clock waits (`_test_slot`) must subtract sleep. Review rejected excluding sleep from runs with an open wait (lost credit, repo-local fact in a machine-wide ledger).
- Ledger `sleep_ledger.observe()` runs at the top of `scheduler_recovery._recover_runs` (that file is a facade split module, 300-line limit). Ledger resets when its baseline is later than now — tests loop with clocks restarting at BASE_TIME in one tmp machine root.
- `tests/conftest.py` stubs `capacity.slept_seconds` to 0 except in `test_machine_sleep`/`test_sleep_ledger`: the owner's real sleep would otherwise credit runs on fake past clocks (BASE_TIME is in the past).
- Review round 3: read the time inside the history/ledger lock (an older reader overwrote newer facts); credit only the least sleep that must lie inside a query (`capacity.sleep_within`) since coarse intervals don't place sleep; an exact kernel pair takes its seconds out of coarse intervals it overlaps (no double source). Clock moving back drops only facts dated after now.
- `store.root.parent` may not exist in unit tests; guard `iterdir()`.

**Why:** these were non-obvious failures found while implementing #415.
**How to apply:** reuse the conftest opt-in pattern for any new real-machine sensor.
