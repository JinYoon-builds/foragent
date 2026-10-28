---
name: project-541-resource-facts
description: #541 D93 machine resource sample per tick and fa orch facts resources object — where it is written/read, test fixture pitfalls
metadata:
  type: project
---

- Each repository's gate writes its own `resource-sample-<store>.json` (one writer per file, D95; readers take the newest `at` via `resource_sample.latest`) in `capacity.machine_root()` from `scheduler_start._apply_budgets()` (cached once per tick). Disk baselines live in the sensor file (`memory-sample.json`, key `disk`) and are read inside `machine_load()` under SENSOR_LOCK; observing mode reads no disk.
- `orch_facts._freeze` copies the sample; when present it replaces the live `machine_load()` read (raw `load_average_per_cpu`). New facts code is in `orch_facts_machine.py` because `orch_facts_resources.py` was already ~300 lines and `scheduler.py` sits exactly at its 250-line cap.
- Acceptance fixture pitfall: the test store is under XDG_STATE_HOME, not machine_root, so the queue counter must add the tick's own store to the stores found under machine_root.
- `GhTracker.rate_limit_reset` sets `rate_limit_remaining`; `orch_github.pause` saves it outside the reset try-block so a write error never discards a valid reset time.
- 27 failures in facts_baseline_trend/test_slots/cli_split/tick_github_budget/union_merge_overlap were identical on the base source (other issues' unimplemented tests). Load base with `git archive HEAD src | tar -x -C .foragent/base` and `PYTHONPATH=.foragent/base/src` (keeps files inside the repo; the goal forbids writing outside it).
- Review lessons: call `psutil.disk_io_counters(nowrap=False)`. The default nowrap=True caches values in the process and hides counter resets. Machine-wide test-slot discovery has one helper, `resource_sample.machine_stores()`, which the gate sample, `_freeze` (copies to `slot-stores/`) and the binding live path all use. Run-budget discovery (`store.root.parent`) stays as it is on purpose.
- Line caps: test_cli_split caps `orch_facts.py` and `orch_facts_resources.py` at 300 lines, and both are close to it on main. Put facts additions in `orch_facts_machine.py` (`freeze_sample`, `freeze_slot_store`). `capacity.py` was already over its 600-line cap on main.
- Review lesson (shared-snapshot ordering): several repositories' ticks must not overwrite one machine file. Use one file per writer and take `at` before the sensors are read. A JSON snapshot with several writers, such as `github-rate-limit.json` (runners plus the gate), is replaced only under the state lock and only when its `seen_at` is newer.
- Merge with #433 (baseline facts): main adds `orch_facts_baselines` and the facts_baseline* messages. Keep both message sets and both imports on one line; `orch_facts.py` is then exactly 300 lines, so do not add any more lines there.
