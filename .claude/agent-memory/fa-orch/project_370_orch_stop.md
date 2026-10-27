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
- **Command order** (`orch.stop`, spec order; round 22 reverted round 21's lock-before-kill, which
  the next review rejected as termination blocked by a runner-held lock): under `claim_lock`
  (`stops/N.lock`, reentrant per thread) refuse/claim and write the request (a takeover keeps
  `saved`/`seen`) → pre-kill `try` (scan_tree, `wait_merge` for a `merging` marker; failure drops
  the request) → re-read run; `_kill_run(outcome="stopped")` only if still running (a run that ended
  meanwhile goes to finish, which reports "runner merged") → `wait_tree_dead` (60 s) →
  `worktree_lock_within` (5 min) → `finish`. All waits use `_attempts`: a deadline on ctx.now(),
  the count only as a backstop for a frozen test clock.
- **Kill-safe runner steps**: `orch_stop_checkpoint.checkpoint` writes `runs/issue-N.checkpoint.json`
  (HEAD, staged tree, branch tip) around `_recreate_worktree`, `_prepare_target`,
  `_restore_replay_source` and both review-state restores; runners have no SIGTERM handler, so a
  kill leaves it and `_save` restores from it (`restore`: update-ref branch, fresh detached
  worktree, `read-tree --reset -u`). Keep a reference to an entered context in tests: GC closes it.
- **Durable evidence**: `scan_tree` writes every process found into the request's `seen`.
  `_save` counts work as saved when `rev-list HEAD --not <origin branch or base>` > 0, not by a
  new commit. Tick decisions that write a run (`_kill_run`, dead-run finish) run in
  `orch_stop.tick_owns(N)`. A dead command + live runner (`runner_alive`) expires; a dead runner
  with descendants left is killed and finished by the tick.
- **finish** (idempotent, also the tick's path): merged (`merged_at` beside the run record, written
  by `run_publish._merge_unless_stopped` under worktree_lock) or closed issue → drop request, change
  nothing. Else save (`_preserve_failed_implementation(stop=True)`: no #572 deletion check, attach
  to `foragent/issue-N` fast-forward only, normal push `HEAD:refs/heads/...`; saved SHA kept in the
  request) → one `stop` event (journal check by started_at) + best-effort comment → finish_run
  stopped → close own PR → ready labels → drop request last. Failures raise `StopStepFailed`.
- **Runner writes after a request** (round 21): `actions.flag` calls `orch_stop.yield_to_stop`
  (raises only when run.pid == this process), and `run_issue`/`review_only` wrap their ctx with
  `guard_runner` (StopGuard tracker: every write checks the request); the public functions turn a
  late `StopRequested` into "stopped". Preservation restores acceptance files by approved blob hash
  (`run_publish._restore_approved_blobs`), never from HEAD. `runner_merged` requires
  merged_at >= started_at. The tick never starts N while any request file exists
  (`scheduler_start` eligible only: importing orch_stop in scheduler.py makes test_scheduler_split
  count orch_stop.py as a 300-line split module). `guard_runner` swaps ctx.tracker in place and
  restores it (main's tests compare the ctx object). Drops pass `owner=record`.
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
- Pitfalls: scheduler_recovery.py is at 299/300 lines, scheduler.py at 250/250. conftest forbids
  porcelain `git commit` in unit tests; build commits with write-tree/commit-tree/update-ref. orch_stop must not import scheduler at
  module level (runner imports it). Tests use a fake process table and a `processes.os` view; never
  patch the real `os.getpid`.
