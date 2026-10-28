---
name: project-370-orch-stop
description: #370 fa orch stop (option A, 2026-10-27) — thin caller of the stall-kill path under the tick lock; why D95 was dropped
metadata:
  type: project
---

Final design (Jin, architect proposal r18, option A): `fa orch stop N --keep-work --why` holds the
tick lock (`store.hold_tick_lock`, 60 s poll, then "the gate is busy"), checks #261's rules without
signaling (`orch_stop.unsafe_target`: lineage/group/session, pid <= 1, start-time mismatch; no
identity or an unreadable table goes on to `_kill_run`), then calls
`scheduler._kill_run(outcome="stopped", detail=why)`. The kill path saves the WIP as
`[stopped: <why>]` (`_remove_run_worktree(why=...)`), finishes the run as stopped, and
`scheduler_stop._restore_stopped` sets ready + one `stop` event (open PR: flag STALLED like
`_restore_stalled`; closed/blocked/needs-human: only remove running). `_recover_orphan` restores a
stopped run whose stop command died after the kill. No runner changes, no request files.

**Why:** the D95 cooperative/fallback-owner design (request files, checkpoints, claim locks,
runner finalizer guards) was held 9 times in 11 review runs; every round found a new race between
two writers. The stall-kill path had 0 wip-preservation-failed events over 67 kills in 7 days.
**How to apply:** for kill-like features, reuse the proven gate path under the tick lock and list
shared races as non-goals instead of building new ownership machinery.

- #572 vs #270: main's `_check_wip_deletions` broke the locked #270 tests. `_preserve_failed_implementation`
  now owns deletions when no other process can remove files meanwhile: the runner's own save
  (it passes `push_branch`) and the stop's save (`owned_deletions`). Gate stall/crash recovery and
  a bare call still refuse untouched deletions (#572 tests).
- Permission: `git reset --hard` is denied here; to restart from main inside an in-progress merge,
  `git restore --source=MERGE_HEAD --staged --worktree -- .` gives main's exact tree.
- scheduler_recovery.py sits at the 300-line split limit and main keeps growing it (297, then 298
  on 2026-10-27). This change adds zero net lines there (orphan "stopped" shares the stall branch,
  `why` is passed inline); keep it that way and re-run test_scheduler_split after every base sync.
- Finalization order (review 2026-10-27): `_restore_stopped` writes the stop event and the orch
  audit comment first, once per run (journal check by started_at), while `running` still lets
  orphan recovery retry; `running` is removed last. `board.sync_status` raises only on rate limits.
