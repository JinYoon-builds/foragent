---
name: project-459-merge-group
description: #459 merge-group acceptance tests contradict short_case's observed_lock green check; QUESTION.md filed 2026-10-26
metadata:
  type: project
---

#459 (merge-group-join): short_case's `observed_lock` starts other runs only inside a lock request (`before_lock`), so a borrowed member always has a pending merge.lock request that is acquired after the innermost leader merged its PR. At acquisition `find_pr` (FakeTracker: open PRs only) returns None → `hold.green=False` → `assert_short_holds` fails. Default/max-2/exclusion tests and the bisect file are unpassable as written; max-1, borrowed-CI-reread and culprit cases are passable.

**Why:** filed `.foragent/QUESTION.md` (ko) recommending a fixture fix (accept a hold whose PR was already merged by another group) instead of implementing against an impossible test.

**How to apply:** if #459 returns with a fixed fixture, design notes: claims must end after the joint check (default test needs 5 to regroup {1,4,5}); after acquiring as non-merged, drop merged members and fall back to today's base-move path, then regroup with unclaimed entries (max-2 test expects [1,6,4,5]); after a failed joint check the leader stays solo for the rest of the run; enqueue only after CI green so held/red runs never appear in queue writes; Config needs `merge_group_max` default 4 (prepare() injects it with object.__setattr__).
