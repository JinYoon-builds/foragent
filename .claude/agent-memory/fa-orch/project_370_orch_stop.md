---
name: project-370-orch-stop
description: #370 fa orch stop — records outcome stopped+why before signaling, reuses scheduler._kill_run, no-write PID safety precheck
metadata:
  type: project
---

- `orch.stop()` holds `store.hold_tick_lock()` (polling up to `STOP_LOCK_WAIT_S`) through the precheck, kill and label changes, then kicks a tick after releasing the lock. Otherwise the dying runner's own kicked tick sees a finished run that still has `running`, treats it as an orphan (crashed) and can relaunch it. Success means the run record finished as `stopped`, not that `_kill_run` returned True. It builds a `scheduler.TickContext` from the RunContext (default `kill_runner=kill_process_group`). It first records `update_run(outcome="stopped", detail="stopped: <why>")` and then calls `scheduler._kill_run(..., outcome="stopped")`. If the kill does not end the run as stopped, it restores the previous outcome/detail.
- **Why record first:** a real runner that gets SIGTERM saves its own WIP (`_execute` finally uses `run.outcome or outcome`) and removes the worktree before `_kill_run` can. Its `run_issue` SystemExit handler also adds `blocked`, unless the recorded outcome is `stalled-restart` or `stopped`. `review_only` has its own SystemExit path, and it honors `stopped` too: no blocked flag, no label change. A caller-only reason parameter on `_kill_run` misses this (review of 2026-10-26).
- `_preserve_failed_implementation` swaps reason `stopped` for `run.detail`, so every saver names the why. `STOPPED` lives in `run_publish.py`.
- Unsafe recorded PIDs are refused with **no** state change, because `_stop_run_processes` writes a `stop-refused` flag. `scheduler_stop.stop_target_refusal(run)` checks first and only reads `processes.list_processes()` and `_protected()`. It also refuses PIDs in `ProcessTable.unreadable`; turning the table into a dict drops those PIDs.
- `--keep-work`/`--why` are argparse-required. Verb wiring lives in `src/foragent/commands/orch.py` (main split `cli.py` into `commands/<group>.py`; on a `cli.py` conflict, take main's file and move the verb).
- Tests: acceptance `test_orch_stop.py` + `test_keep_unfinished_work.py`; boundary `tests/test_orch_stop_unit.py`, `tests/test_runner.py::test_runner_stop_leaves_orchestrator_stop_labels_and_reason_to_the_verb` (the run_case teardown requires `running` removed).
