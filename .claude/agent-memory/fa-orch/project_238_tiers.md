---
name: project-238-tiers
description: #238 per-attempt tier choice by run executor's usable quota; where model/effort are chosen and which old tests depended on tier order
metadata:
  type: project
---

- `run_implementation.choose_tier()` picks each attempt's tier: stored tier for a resumed attempt number, else `attempt_tier("l" if ample else size, attempts)`, forced `l` on `attempts >= max_retries + 1`, never below an earlier `attempt_tiers` value. `ample_quota()` reads `ctx.config.executor` (already the stored pair via `stored_config`), skips non-subscription entries (`scheduler_quota.entry_for`, API-key Claude), and uses `quota_headroom.usable()` (plain fraction) with the shared `quota_headroom.cap()`.
- `cap()` now prefers an explicit executors-list keep_percent (burn does not lift it); #186's `order()` shares it.
- `tier_model_effort()`: lane model only while `config.executor_model == lane model` (pair_config clears it on a switched executor), then tier `<executor>_model` ('' → no flag, None → executor_model). Claude effort goes to the CLI as `--effort`.
- **Why it matters:** `run_case` uses `max_retries=1`, so attempt 2 is now always `l`. Old tests expecting s→m with one retry need `max_retries=2`. Fake ctx objects that drive `_implement` need `now` (tests/test_order.py `_implementation_setup`).
- Review r2: conflict-resolution turns never advance `run.attempts`, so they get their own `attempt_tiers` key `"<attempt>c<conflict_attempts>"`; the turn number is `max(attempts, earlier keys + 1)`, a rebuilt attempt (`attempt.rebuilt`) is final → l. PR body renders `Attempt 2, conflict turn 1 at tier l`. Executors other than codex/claude without `<name>_model/_effort` keys (`has_tier_keys`) get no model/effort flag and stay at the size tier (no ample, no step-up).
- Review r3: per-turn options are merged fresh each turn (`{**attempt.options, **setup.options}`), never `update()`d, so a tier with effort None sends no effort. Resume identity is the stored `Run.open_turn` (set with the tier, cleared after a normal result, carried only on limit-wait `start_run`); `turn_key()` derives conflict keys from stored `attempt_tiers`, and `choose_tier` returns a stored tier only for the open turn. The in-memory conflict budget (`attempt.conflict_attempts`) still resets on restart (pre-#238 behavior).
- Boundary tests: `tests/test_tier_choice_unit.py`. Related runner files (21 files, ~18 min) pass; run them in background — a foreground `fa test` over them exceeds the 10-minute tool limit.
