---
name: project-238-tiers
description: #238 derived tier per executor turn (architect r42); where model/effort are chosen; the five review rounds that a lookup design kept failing
metadata:
  type: project
---

- Final design (architect r42, 2026-10-30): `run_implementation.choose_tier(ctx, run, size) -> (key, tier)`. Turn k = `len(run.attempt_tiers) + 1` at every executor turn (implementation, conflict, CI repair, rebuild, a turn again after limit-wait/restart); the runner writes `attempt_tiers[str(k)]` once and never rewrites. Base = `attempt_tiers["1"]` if a valid tier, else `l` when `ample_quota()` else the size. Tier = `l` when `attempts >= max_retries + 1`, else `attempt_tier(base, k)`. `scheduler_start` no longer passes `tier` to `start_run`.
- **Why:** rounds r2–r5 patched a lookup design (per-attempt keys, conflict keys `1c2`, `open_turn`, floor, attempts carried on crash) and each fix exposed another turn-identity collision. Deriving the tier from a monotonic count removed the whole class. **How to apply:** when a review keeps finding identity/resume edge cases, propose deriving the value from an append-only record instead of adding rules.
- `start_run` carries `tier`/`attempt_tiers` on every restart outcome (stalled-restart, crashed, limit-wait); only limit-wait keeps `attempts` (#84 budget).
- `ample_quota()` reads `ctx.config.executor` (the stored pair), skips non-subscription entries and executors without tier keys, uses `quota_headroom.usable()` with the shared `quota_headroom.cap()` (executors-list keep_percent first; burn lifts only claude_quota).
- `tier_model_effort()`: lane model only while `config.executor_model == lane model`; tier `<executor>_model` ('' → no flag, None → executor_model); an executor without tier keys gets no model/effort. Per-turn options are merged fresh (`{**attempt.options, **setup.options}`), never `update()`d.
- Tests: `run_case` uses `max_retries=1`, so turn 2 is always `l`. Fake ctx driving `_implement` needs `now`. Boundary tests in `tests/test_tier_choice_unit.py`. `tests/acceptance/test_related_tests_record.py` timeout cases fail before and after (another order). Run the related runner files in the background; a foreground `fa test` over them exceeds the 10-minute tool limit, and `.foragent/` may be missing after a worktree rebuild (mkdir first).
