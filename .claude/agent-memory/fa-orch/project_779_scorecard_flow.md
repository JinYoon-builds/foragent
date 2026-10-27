---
name: project-779-scorecard-flow
description: D96 flow metrics in fa orch scorecard (#779) — module layout, binding events file, line limits hit, test pitfalls
metadata:
  type: project
---

- Flow metrics live in `orch_scorecard_flow.py` (`daily()` per bucket, `previous()` daily medians, `worse_names()`); `orch_scorecard.collect(store, days, lanes)` merges them. `worst()` skips `flow.NAMES` so fa brief keeps its line.
- Binding events go to `<state>/binding-events.jsonl`, NOT orch-events.jsonl: 56 tests in `tests/test_scheduler.py` assert exact orch journal kinds. **Why:** any new per-tick journal event breaks them. **How to apply:** new per-tick records get their own file.
- The tick writer is called from `orch_spawn.deciding()` normal exit (every acquired tick passes there); `scheduler.py` was exactly at its 250-line limit (test_scheduler_split), so no lines could be added there.
- SKILL.md of fa-orchestrator has no trailing newline: `wc -l` 149 = 150 splitlines lines (the limit). Rewrap with backtick-aware tokens; width up to ~144 is within existing lines.
- Idle minutes: segment sweep over change points, midpoint state; test-slot holding phase = `checking`; prepared run starts (`store.run_starts()`) occupy a slot until a run of that issue starts.
- Verified: Done-when 45, related 62 + scheduler/spawn/inbox 306 pass; skill-reading acceptance set identical before/after.
- Superseded retry note: an own `rule()` mirrored facts' `_admission`+`_binding` (cap ends the loop before lane holds; quota > test_slots > cap > structure; github only holds waiting issues). Tick and the scorecard's per-day replay (`flow.binding_at`, day end, phase ends inclusive) both call it.
- Machine-wide budgets: idle occupancy counts sibling stores under `store.root.parent` by facts' rule (pid None → launch grace only). make_case's `tmp_path/state` store is a sibling in scorecard acceptance tests and stays uncounted only because its run has no pid.
- Pitfall: a recreated worktree has no `.foragent/`; break-it backups copied there silently failed and mutations accumulated. Create the backup dir first and check `cp` succeeded.
- Retry 2: reimplementing facts' binding kept diverging (quota, cap, file rule). Root fix: the tick calls facts' `_resources`+`_binding` directly (machine dict = tick sample, alive {}, machine_budget.values(), read_test_slots()); the replay `flow.binding_at` reuses `_ranked`, `admits`, `approved_issue` and `_binding`. Open holds use `open_end` past the replayed moment.
- Without label events, never project current labels backward: startable from approved_at; holds count from `load_flag().flagged_at`; merged blockers block until their merged_at. Each such time must also be a change point in the idle sweep.
- Retry 3: Run records, history included, keep `files_committed`/`effective_files`; a replay must claim through `overlap_files.claim()` (shared with `claims()`). Do not rebut "no record exists" without grepping `state_records.Run` first.
- `tests/acceptance/test_union_merge_overlap.py::..._normal_file_still_waits...` fails independently (stand-in `_credited_wait` arity 3 vs 4), 2026-10-27.
- Retry 4: replay recorded intervals by record, never by tick-observation time. Plan pauses: a later pause of the same lane replaces the earlier one; a pause ends at min(until, replace/resume time, its plan's latest `plan-expires` value, or `plan-expired`'s `expires_at`). Merged-ness is by effect: a recorded merge (merge_times >= started) counts as merged whatever the outcome label.
- The worktree is recreated between retries: `.foragent/TRY.md` and `REBUTTAL.md` vanish, so rewrite them every retry.
- Retry 5: binding replay candidates = facts' `_waiting_issues` + `startable` exactly (approval snapshot required, NO blocked_by rule). Idle keeps its own rule (blocker rule locked by acceptance). Keep the two filters separate (`_startable(..., blockers=False)`).
- Retry 6: the tick binding passes `probe=False` through facts' `_resources`/`_admission`, which sets `ctx.case_sensitive=None`; `overlap_files.intersects` honours a ctx answer instead of calling `_filesystem_case_sensitive` (ranking and admits both go through intersects). Without label events, a current label applies only from the row's `updated_at` (`update_local_issue` stamps it on every local write) or the flag time.
- facts acceptance files `test_facts_baseline_trend`/`test_facts_repeated_work`/`test_resource_facts` had 32 independent failures on 2026-10-27 (same set with HEAD sources).
