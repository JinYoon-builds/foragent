---
name: project-370-orch-stop
description: #370 fa orch stop under D95 — one owner per run; request file first, kill, wait for dead tree and worktree_lock, then the stop is the fallback writer
metadata:
  type: project
---

Design (2026-10-27, D95 redesign; replaces rounds 1–20, which were held 7+ times for one defect
class: two writers on one run):

- **Ownership.** While its process lives, the runner alone writes the run record, worktree and
  labels. `fa orch stop` is a fallback writer that acts only after the whole tree is dead.
- **Request** `<state>/stops/N.json` (`orch_stop.write_request`): number, started_at, why,
  requested_at, the stop command's pid and start_time. `stop_request()` matches it to the current
  run by started_at, so an old run's request never stops a new run.
- **Command order** (`orch.stop`): refuse (not running, batch branch, live other stop command,
  `scheduler_stop.stop_target_refusal`) → write request → one `scheduler._kill_run(outcome="stopped")`
  (that branch only terminates; "gone" counts as success) → `wait_tree_dead` (60 s) →
  `worktree_lock_within` (5 min, non-blocking tries) → `orch_stop.finish`. Waits poll with the
  RunContext `sleep`/`now`, 1 s steps, bounded by count too (a fixed test clock cannot loop).
- **finish** (idempotent, also the tick's path): merged (`merged_at` beside the run record, written
  by `run_publish._merge_unless_stopped` under worktree_lock) or closed issue → drop request, change
  nothing. Else save (`_preserve_failed_implementation(stop=True)`: no #572 deletion check, attach
  to `foragent/issue-N` fast-forward only, normal push `HEAD:refs/heads/...`; saved SHA kept in the
  request) → one `stop` event (journal check by started_at) + best-effort comment → finish_run
  stopped → close own PR → ready labels → drop request last. Failures raise `StopStepFailed`.
- **Runner**: `orch_stop.stop_owns()` (request exists and not merged) at every finalizer of
  `run_issue`/`review_only`/`_implement_and_publish` (publish, flag, preservation, worktree
  removal, finish). `leave_to_stop` raises `StopRequested` (an `ExecutorStopped("stopped")`).
  run_issue returns "stopped"; the CLI maps non-"error" to exit 0.
- **Locks**: `worktree_lock` is reentrant per thread (`state_locks._held_worktree_locks`), plus
  `try_worktree_lock`. Destructive transitions hold it at the outermost level: `_recreate_worktree`,
  `_prepare_target` (recreate → apply patch), `_restore_replay_source` (recreate → read-tree),
  review-state restoration in run_review.
- **Tick**: `orch_stop.recover_requests` first in `_recover_runs`; returns issues to skip. Live
  command → skip. Dead command + live tree → `stop-request-expired` ("Removed a stale stop request
  for #N"). Dead command + dead tree, or run finished as stopped → finish. Finished otherwise or
  missing → expire after 10 min from requested_at. `_kill_run` (non-stop outcomes) and
  `_recover_orphan` leave a run with a current request alone.
- **#572 vs #270**: main's deletion check breaks the locked #270 tests; `Run.live_changes` (the only
  Run field added) is recorded by `run_publish.observe_live_changes` at each implementer turn end
  and owns those deletions.
- Pitfalls: scheduler_recovery.py is at 299/300 lines. orch_stop must not import scheduler at
  module level (runner imports it). Tests use a fake process table and a `processes.os` view; never
  patch the real `os.getpid`.
