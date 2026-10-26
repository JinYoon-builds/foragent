---
name: project-370-orch-stop
description: #370 fa orch stop — records outcome stopped+why before signaling, reuses scheduler._kill_run, no-write PID safety precheck
metadata:
  type: project
---

Current design (2026-10-26, after review round 9):
- `orch.stop()` holds the tick lock (`STOP_LOCK_WAIT_S` poll) from the precheck to the labels, then kicks a tick. `stop_target_refusal` reads processes without any write. It refuses unsafe, unreadable or unrecorded runner/executor PIDs, including `run.pid is None` and a pid that is not in `process_identities`.
- Intent: `update_run(outcome="stopped", detail, stop_why=why)` runs under `run_publish.stop_intent_lock(store, n)` (`runs/<n>.stop.lock`; the state lock is not reentrant). Only orch writes `Run.stop_why`. `_preserve_failed_implementation` re-reads the run under the same lock at commit time, names `stopped: <why>` if the intent is there, and records `Run.wip_reason`.
- Success means the run finished as `stopped` and `stop_kept_work(run)` holds: `wip_reason` is None or names the why. `final_outcome()` in the runners applies the same rule, and merged and wip-not-saved always win. A run that saved WIP for its own reason before the intent keeps its own outcome, and the stop reports failure.
- Destination: `scheduler_stop.complete_stop(ctx, run)` journals the required `stop` event first, once per run (idempotent check against `started_at`). It then closes this issue's own open PR, removes blocked, needs-human and running, and adds ready (retry semantics). A closed issue only loses running. If this fails, orch raises `StopPending` and kicks a tick; `_recover_orphan` repeats `complete_stop` for a finished stopped run when `stop_kept_work` holds. The GitHub comment is best effort, sent after the journal.
- Durable completion record (round 9, defect class "recovery depends on a label an earlier step erased"): `scheduler_stop.mark_stop_pending` writes `<state>/stops/<n>.json` (number, started_at, why) inside the intent lock, before the intent. `complete_stop` removes each label in its own call and deletes the record only after every step succeeds. `recover_pending_stops` (first line of `_recover_runs`) drives each record. A finished stopped run whose work was kept is completed; other runs drop the record; running runs are left to the live/dead branches. Never drive stop recovery from the `running` label: a dying runner's `flag()` removes it first.
- No flag suppression: labels are decided after every process is gone, so a dying runner's flags are overwritten by `complete_stop`. Earlier designs that blocked on an open PR, kept holds, or suppressed flags were rejected by review as alternate destinations or stale-read races.
- WIP loss: `keep_unsaved_work` reports best effort and never raises. A failed save keeps the worktree and records `wip-not-saved` (`_kill_run`, `_finish_dead_run`, `_recover_orphan`, `run_issue`, `review_only` CI repair).
- `clear_stale_stop` (tick, live-run branch) drops an intent left by a stop process that died before signaling.
- Runner SIGTERM handlers (`run_issue`, `review_only`) leave labels to the stop when `stop_why` is set.
- `--keep-work`/`--why` are argparse-required; the verb lives in `commands/orch.py`.
- Limits and pitfalls: `scheduler_recovery.py` is near the 300-line split limit, so add helpers to `scheduler_stop.py`. `.foragent/acceptance-exclusions.json` is owned by the runner and untracked since 502b4c8. For a base comparison, use the merge's second parent, not `HEAD^`.
- Tests: acceptance `test_orch_stop.py` and `test_keep_unfinished_work.py`; boundary tests in `tests/test_orch_stop_unit.py` plus stop tests in `test_runner.py` and `test_orch.py`. The run_case teardown requires `running` to be removed.
- 2026-10-26 #572 conflict resolved by me: `_check_wip_deletions` refuses unrecorded deletions only at `MASS_DELETION = 10` or more, because the locked #270/#370 checks keep a single implementer deletion. Three #572 unit tests now delete ten files.
