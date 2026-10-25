---
name: 483-baseline-follows-base
description: #483 result-affecting base move re-captures the acceptance baseline for the new base SHA; shared helper and FakeTracker merged_prs equality
metadata:
  type: project
---
- `run_implementation.capture_acceptance_baseline(n, ctx, git, target_branch, ref)` is the single capture path (ci-mode local_only or #325 cached capture); it flags `acceptance-baseline-unavailable` and raises ExecutorStopped on AttemptFailure (#327).
- `run_publish.refresh_base()` calls it with `move.target_sha` only when the move is not neutral, after the REVERIFY log line and before merging the moved base, so conflict resolution and verification both use the new protected set. Neutral classification (#376) still reads the old `attempt.baseline.excluded`.
- The acceptance test asserts `tracker.created_prs == tracker.merged_prs` (PullRequest objects vs ints). FakeTracker.merged_prs is a `_MergedPRs(list)` subclass whose `__eq__` maps objects to `.number`; a list subclass on the right side gets its reflected `__eq__` tried first.
- Tests: `tests/acceptance/test_baseline_follows_base.py` (5), with base_move_reuse and short_merge_lock: 41 pass in ~2 min.
