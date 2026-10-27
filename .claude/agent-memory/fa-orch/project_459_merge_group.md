---
name: project-459-merge-group
description: #459 merge groups — queue/claim design that the acceptance tests require, and pitfalls hit while implementing (2026-10-26)
metadata:
  type: project
---

#459 is implemented in `run_merge_group.py` (form/joint check/member re-read/merge), `state_merge_queue.py` (merge-queue.json, claims) and the lock loop in `run_publish._publish_locked`. The first QUESTION (borrowed members' late empty hold was not green) was right; the order fixed the harness in 7ed16d1.

**Why:** the tests fix behaviour that is not obvious from the issue text.

**How to apply:**
- Claims end right after the joint check; otherwise a newer leader cannot regroup the full set (the default test expects one group of 1, 4, 5).
- When the group's base moved at the final lock and unclaimed peers exist, regroup on the moved main without refreshing the own head. The max-2 test compares queue heads with the *final* PR heads. The pre-lock base refresh is also skipped while unclaimed peers exist, and a passed group's pinned base counts as the checked base.
- After a failed joint check, the leader stays solo for the rest of the run.
- Dequeue the leader's own entry under the lock just *before* merge_pr. A thread waiting on the merge signal otherwise sees it as a peer (test_run_publish parallel-lanes test).
- Unit fixtures wrap `_merge_lock` and yield None. Pass the hold through the `_HOLDS[n]` registry, not through `as hold`.
- `needs-human` must be a merge-decision hold (`policy.merge_decision`); the exclusion test expects that run to be "held".
- The runner can be killed (SIGTERM) and recreate the worktree. Only its WIP commit keeps the work, so do not rely on uncommitted files between attempts.
- Review round 1 (2026-10-26) fixed five defect classes: (1) stale evidence — merge borrowed members only when *all* re-read valid; otherwise the leader merges alone only on its own verified base and does not regroup that turn (test 5 would loop otherwise); (2) joint checks must leave HEAD and the staged tree unchanged; (3) a run claimed by another leader cannot claim or report peers; (4) gate grouping on `config.checks == "local"`, not `ci_mode.enabled()` (that one is capability detection); (5) make each member merge durable at once (`group_merged_head`) and finish members in `finally`; a member that sees the marker closes its own issue if still open.
- Keep test output in harness background tasks only; Stop hooks count shell redirection to /private/tmp as changing files outside the repo.
- Review round 2 (2026-10-27): queue entries, claims and the `group_merged` run-record marker carry the execution's `started_at`, so a restarted issue never inherits them; every queue reader saves its cleanup. `finish_group()` writes local facts → dequeue → reinstall → journal `merge` events before any GitHub call, and attempts each member's close/labels/board independently (never raises); a member that sees the marker swallows its own close failure and still returns "merged", so it is never re-implemented.
- Review round 3 (2026-10-27): `merge_in_order()` never raises; the caller finishes every merge first, then re-raises only if the leader did not merge. A borrowed member's failed closure goes to the existing GitHub outbox (`queue_github_write` close_issue + remove_labels; `flush_outbox` in the tick delivers) plus a `close-pending` event. The leader's own close keeps #210's rate-limit propagation. Joint checks drop peers that went stale (`merge_entry_current`, worktree, branch fetch GitError) instead of failing the leader. Member `merge_lock_held_seconds` is written inside `_merge_lock` before release.
- Review round 4 (2026-10-27): every queue call is guarded (`queue_in_use(ctx)` / `grouping`), and `_save_queue` never creates an empty file, so `checks: ci` never touches merge-queue.json. Stale peers (missing worktree, restarted run, deleted branch) are dequeued when dropped, and `_live_entries` ignores entries whose worktree is gone, so a ready leader never loops on them. `merged_by_group()` runs before each fallible publish step (loop top, base refresh, after form, in the lock); `form()` treats its own branch fetch failure as "merged elsewhere" when the marker exists.
- Review round 5 (2026-10-27): a group on a main newer than a member's baseline (`baseline_base_sha`) adds a fresh `_capture_acceptance_baseline` on the pinned main (capture failure fails the joint check → one at a time); regrouping skips today's push+CI, so this is what protects tests newly passing on the moved main. Queue entries store each member's own `unit_command`/`unit_tests`; `_unit_runs()` runs their union plus the combined selection, once per command. `_finish_if_group_merged()` outside the lock first acquires merge.lock (the leader saves facts before releasing) — never finish a group-merged run without it.
