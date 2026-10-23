---
name: issue-106-capacity-aimd
description: #106 machine-wide AIMD run budget — where state lives, the locked-test conflict with load_config's missing-file guard, and scheduler test pinning
metadata:
  type: project
---

#106 (D60 1) adds `src/foragent/capacity.py` (next_parallel, machine_load, machine_runs, machine_lock, capacity.json/capacity.lock under `store.root.parent` = ~/.local/share/foragent). scheduler imports the names so tests can patch `scheduler.machine_load`/`scheduler.next_parallel`.

**Why:** the locked acceptance test patches those scheduler globals and checks a file directly under the data root changes.

**How to apply:**
- Machine root is `store.root.parent`, not `Path.home()`, so unit tests with `StateStore(tmp_path/"state")` stay isolated from real live runs.
- `tests/test_scheduler.py` ctx fixture pins `machine_load` to ("normal", 0.0); otherwise real load/pressure makes scheduler tests flaky.
- The locked test once called `load_config` on an empty folder; it was fixed upstream (4af3569) to write `.foragent.yml` first. Keep the "not a Foragent repo" guard.
- Review fixes: `max_parallel` is a per-repository ceiling (own active runs), never a machine cap. The stored AIMD value is only pressure/load-driven (`lanes_with_work=max(demand, current)`); the lanes cap applies to the tick budget only. Pid-less runs and `run-starts` reservations from other repos hold a slot only within `LAUNCH_GRACE` (2 min, shared with `_recover_runs`). The machine lock covers only recount + `prepare_run_start`; GitHub calls happen outside.
- Full unit suite takes ~620s, over the 600s foreground limit; split it into two foreground halves instead of a background run.
