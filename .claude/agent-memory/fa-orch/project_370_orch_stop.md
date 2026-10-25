---
name: project-370-orch-stop
description: #370 fa orch stop — reuses scheduler._kill_run with outcome=stopped, safety precheck before any state change
metadata:
  type: project
---

- `orch.stop()` builds a `scheduler.TickContext` from the RunContext (default `kill_runner=kill_process_group`) and calls `scheduler._kill_run(..., outcome="stopped", wip_reason=...)`. `wip_reason` flows to `_remove_run_worktree` → `_preserve_failed_implementation` so the WIP commit message names the why. It is passed only when set, so fakes with the old signature still work.
- Unsafe recorded PIDs must be refused with **no** state change, but `_stop_run_processes` writes a `stop-refused` flag on refusal. So `scheduler_stop.stop_target_refusal(run)` checks first, reading only `processes.list_processes()` + `_protected()`: a live pid/executor_pgid must match a recorded identity start time and lie outside lineage/group/session.
- `--keep-work` and `--why` are argparse-required, so a missing `--why` fails before any context is built.
- **Why:** acceptance `test_orch_stop.py` snapshots state, refs, files, events and outbox around each refusal.
- **How to apply:** for later orch verbs that kill runs, reuse this precheck + `_kill_run` and do not write a parallel kill path.
