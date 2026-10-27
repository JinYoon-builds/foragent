---
name: project-786-executors
description: #786 ordered executors list — DerivedExecutors list subclass, decide() fallback order, spend.jsonl, credits question key
metadata:
  type: project
---
- `Config.executors` is always a list. When the repo file has no `executors`, it is a `DerivedExecutors` (list subclass) that `__post_init__` re-derives on every `replace()`, so `lane_config()` gives a lane its own legacy list ([claude, codex] for claude, else [executor]). `executors_explicit(config)` tells them apart. `asdict()`/`deepcopy` keep the subclass, so DEFAULTS stays derived.
- File-scoped rules (global `executors`, `claude_quota`+`executors` in one file, repo `executor` ≠ first) live in `config._apply_executors()`, not `_validate()` (which only sees merged values).
- `scheduler_quota.decide()` walks `fallback_order()` with `blocked()`; credits question via `orch_inbox.ask_once(key="credits:<name>")` only when nothing could start. Spend: `StateStore.record_spend/spend_today` (UTC day, append-only `spend.jsonl`); runner records via `run_execution._record_spend` → `scheduler_quota.record_cost`.
- Config accepts api on any executor (orchestrator answer after a spec-conflict stop); decide() holds unmeasured api executors (`MEASURED`) with `Start.reason`.
- Runner must apply the same rules per session: `run_execution._session_hold()` uses `scheduler_quota.blocked(keep=False)` (cap/unmeasured before pauses; capped subscription credits skip `hold_lane`). Spend is charged per session via `credits()` at launch, not from run.started_at. Runner reviewer comes from `scheduler_quota.run_config()` so it matches decide().
- Review attempt 1 failed on 'decide vs runner disagree' and 'cap rules skipped for reviewers' — check both sides whenever admission rules change.
- `tests/test_config.py` exact-defaults tests compare `asdict(config)`; new Config fields must be added there.
- tests/acceptance/test_settings_* fail on base (no `fa settings` command yet) — not a regression signal.
- Review attempt 2: admission must sit inside `_available_executor.execute()` (every launch: failed-resume relaunch, retries, reviewers), not the loop head. Reviewer choice keeps only `can_review` names, else `none`. `executors: null` is a ConfigError; `run_base_move._repo_config` must call `_apply_executors` too (third config-loading path).
- Break-it pitfall: a mutation guarded by `hold is not None and f()` never calls f on the first launch; mutate on a launch property (e.g. `"resume_session_id" in kwargs`) instead.
- Review attempt 3: every hold path (implementer, reviewer, lane) must ask credits when a hard limit is the cause; continuations use blocked(keep=False).
