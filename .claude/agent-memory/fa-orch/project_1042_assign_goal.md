---
name: project-1042-assign-goal
description: D80 per-issue executor/model assignment and Jin's goal — storage in plan.json, decide() hook, run record field, test baseline notes
metadata:
  type: project
---

- `orch_assign.py` holds rules + CLI for `fa orch assign N --executor --model [--until] | --clear` and `fa orch goal TEXT --deadline | --clear` (both need `--why`, `--by` default human). Stored in `plan.json` keys `assignments` / `goal` via `orch_plan._edit` (author protection, one journal event each: plan-assign, plan-assign-clear, plan-goal, plan-goal-clear).
- Plan expiry does NOT lapse assignments/goal (`_in_force` keeps them); an assignment lapses at its own `until`.
- Applied in `scheduler_quota.decide()`: order = [assigned entry], no headroom ranking, no kept implementer, keep_percent off (like a continued run), hard limits still hold; `Start.model` → `start_run(executor_model=)` → `Run.executor_model` → `run_implementation._implement` model override. Runner's `stored_config` drops a non-lane executor's model, which is why the model rides on the run record.
- Acceptance test's release case passes even without the decide override (lane executor is already codex); `tests/test_orch_assign_unit.py` assigns claude to cover it.
- Skill-reading acceptance files: 18 failures in architect_project_schedule / orch_say / orchestrator_retrospective exist on base too (2026-10-31).

**Why:** future D80 follow-ups touch the same plan file and start decision.
**How to apply:** reuse `orch_plan._edit`/`_check_author` for new plan controls; keep per-run choices on the run record.
- Review fix: an assigned model must also win during recovery. `quota_reviewer.assigned_model()` sets `executor_model` and clears `executor_fallback_model` in both `stored_config` (runner) and `decide` (tick), so run_execution's transient fallback never fires. "Configured" ≠ "runnable": `orch_assign.runnable()` asks `executors.executor_for`; decide ignores a stored assignment without an adapter.
