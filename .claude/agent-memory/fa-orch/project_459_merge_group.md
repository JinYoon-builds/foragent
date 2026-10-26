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
