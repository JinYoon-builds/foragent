---
name: project-712-merge-turn
description: #712 FIFO merge turn in run_merge_turn.py over <state>/merge-queue.jsonl; how the queue is read, where publish paths join/withdraw, and test pitfalls
metadata:
  type: project
---

#712 replaced the 3-merge "base kept moving" cap with a FIFO merge turn (`src/foragent/run_merge_turn.py`).

- Reader: key = (n, started_at); first `ready` fixes position, only `withdrawn` ends it; head = first remaining entry whose run is `running` (own key always counts). A `turn` record is history only: ending the position there handed the turn over while merge_pr was still in flight (review finding, fixed). Missing `target`/`started_at` = legacy rows (match any target; live if issue has a running run).
- The turn record is appended inside the merge lock right before `merge_pr` (acceptance test checks the record is already in the file when merge_pr is called), with result "merged"; a TrackerError appends a second record with result "failed".
- Both `_publish_locked` and `_publish_reviewed_head` are thin wrappers: `try: outcome = _publish_*_in_turn(...) finally: withdraw unless outcome == "merged"`.
- Review-only holder must refresh a moved base, not stop base-changed (review finding): `refresh()` merges the base into the detached head, calls `reverify` (`_verify_review_head` passed from run_review), pushes with `GitRepo.push_head(branch, lease=old head)`. `unchanged()` no longer compares base SHA and `_final_merge_inputs` is called without `base_sha`. The fake push in `tests/runner_support.py` moves the PR head on a `HEAD:refs/heads/<pr branch>` push.
- Join happens after the first green CI (the acceptance test needs every run to poll CI before queuing). A run that actually waited re-checks CI with a fresh `_CIWait` (deadline starts at the turn). Wait phase reuses `merge-lock-wait` (already rendered as "waiting to merge"); the wait is not credited as waited time.
- `merge_turn_size` lives in Config; validation rejects anything but int 1. `tests/test_config.py::test_exact_defaults` lists every default.
- Test pitfalls: a fake `pr_checks` that moves the base on every poll now loops forever when the fake clock is frozen (only `_remaining` stops base churn), e.g. `test_orch.py::test_rechecks_evidence_after_ci[base]`; move it once. Tests that pin merge-lock scope with two threads must patch `run_merge_turn.holds_turn` to True, or B waits in the FIFO and never reaches the lock. macOS has no `timeout`; a test file running >10 min is a hang, find it with `-v` output to a file.

**Why:** 38% of runtime was lost to base-churn stops (O(N²) re-verification).
**How to apply:** #459 (merge groups) extends `members` and `merge_turn_size`; keep the reader rules above. `tests/acceptance/test_run_publish_split.py` (#560) caps run_publish.py at 250 lines and fails on base; not this issue's scope.
