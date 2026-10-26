---
name: project-123-baseline-survivors
description: #123 dropped-id audit after a base sync, built on #483's follow_base; history of the rejected per-id probing designs
metadata:
  type: project
---

Final spec (2026-10-27): #483's `run_implementation.follow_base()` owns the protected set after a base move. #123 adds, in the
same function: `run_baseline.collect_protected_ids()` collects the previously protected ids on a detached checkout of the new base
(`--collect-only <ids>` + the new base's `--ignore` exclusions, capture time limit, run budget, test slot; exit 0 = all found,
exit 4 = bisect to the missing ones, anything else/timeout → #327 stop via `_baseline_unavailable`). Exit 4 also means a
SyntaxError/ImportError under a file::test selector (the file alone then exits 2), so a single exit-4 id is missing only if the
same command then collects its file (or, when the file is gone, the nearest directory left — that loads a broken conftest.py,
which also exits 4) with 0/5; a missing `-p` plugin exits 1. No JUnit anywhere in this step: the
locked #483 harness (`test_baseline_follows_base.py`) counts every `--junitxml` command as a capture ("captured exactly once"). No id left → no re-capture,
empty baseline. Emptiness is derived, never stored (`attempt.baseline` exists with no node_ids, whether empty from the start or
after a sync or a re-capture that protects none) → later syncs skip re-capture. The verification-skip line is logged by
`follow_base` at the moment a sync empties the set; `run_checks` stays as on main (a main test asserts no verification line
for a baseline that starts empty). Otherwise
the re-capture runs and `log_dropped_ids()` writes one English audit line per id protected before and not after:
owned by an open issue > missing on the new base > failing on the new base > no longer protected on the new base (CI mode,
collected_ids None).

**Why:** ten review rounds of per-id pytest probing (exit codes, JUnit, deselects, controls) each found a new forged or edge signal;
the orchestrator then rewrote the spec to rely on #483's re-derived set and drop per-id probing. Don't reintroduce it.
**How to apply:** prebuild's locked test `test_main_move_prebuilds_new_sha_while_old_run_keeps_old_baseline` already failed on main
(6ea1f52) when this landed — compare against main before treating it as a regression. Locked prebuild harness forbids `--junitxml`
commands in the issue worktree and wants the verification's `::` words to equal the protected ids.
