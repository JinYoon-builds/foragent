---
name: project-1027-file-overlap
description: #1027 per-file overlap limits in plan.json (fa orch plan --overlap), overlap_mode, and the locked-test setup defects raised as QUESTION
metadata:
  type: project
---

#1027: `orch_plan` stores `files: {path: {limit, why, by, expires_at}}`; each entry has its own expiry, so the plan's
`expires_at` does not end files and a file edit never touches queues/width/expiry. `normal_file()` uses posixpath.normpath,
refuses absolute/`..`/glob paths. `overlap_files.limit_for()` gives a concrete path min(intersecting entries) else
`overlap_per_file`; a glob takes min(fallback, entries). Same `at_capacity` serves admission and run_overlap's late
boundary. `overlap_mode: measured|fixed` (spec name is `measured`, not `plan`). Unit tests `tests/test_overlap_plan_unit.py`.

**Why:** three tests in the locked `test_measured_overlap_plan.py` fail in setup, before the feature: body edit after
approval at the frozen clock (`>=` in scheduler_approval:76 → hash-mismatch), a 2h clock jump past the 100-minute runtime
kill, and `replace(load_config(...))` dropping `approvers=["human"]`. Raised as QUESTION (recommend fixing test setup).

**How to apply:** when a locked acceptance test fails, first rerun the same flow in a scratch copy with only setup fixed;
the frozen `ctx` clock (NOW) makes any post-approval body edit invalidate approval, and any time jump >100 min kills runs.
