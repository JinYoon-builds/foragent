---
name: project-470-memory-backoff
description: #470 sustained memory back-off — machine_load window state in memory-sample.json, hold flag, rule fields, floors removed
metadata:
  type: project
---

`capacity.machine_load()` keeps window state in `<data root>/memory-sample.json`
(swapins_total baseline, window_started_at, swap_windows streak, last_swapins, pressure_since).
It reads time through module-level `datetime.now(UTC)` because the acceptance fixtures patch
`capacity.datetime` (and the fresh-process test does too), so never add a `now` parameter.
A call inside an open 30 s window keeps the baseline; kernel warn/critical is recorded on every call.

**Why:** the acceptance tests drive 0-second ticks and multiple repos; only a machine-wide,
persisted window clock satisfies "repeated ticks neither complete nor reset a window".

**How to apply:** `LoadSample` carries `rule`, `swap_windows`, `pressure_windows`, `hold` as
attributes so it still equals the `(pressure, load)` tuple; callers use `getattr(..., default)`
because capacity-aimd fixtures stub plain tuples. `hold` blocks growth only. Decrease rows get
`capacity.decrease_fields(sample)` (cpu-load when pressure is normal). Floors are gone:
Config keeps the two fields (unknown keys are rejected) without validation, machine_budget lists
them in `IGNORED_KEYS`. Unit tests that pin machine_load need a patched `capacity.datetime`
(see `sensor_clock` in tests/test_capacity.py). Related: [[project-106-capacity]], [[project-243-test-slots]].
