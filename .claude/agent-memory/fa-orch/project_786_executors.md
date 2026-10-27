---
name: project-786-executors
description: #786 ordered executors list — DerivedExecutors list subclass, decide() fallback order, spend.jsonl, credits question key
metadata:
  type: project
---
- `Config.executors` is always a list. When the repo file has no `executors`, it is a `DerivedExecutors` (list subclass) that `__post_init__` re-derives on every `replace()`, so `lane_config()` gives a lane its own legacy list ([claude, codex] for claude, else [executor]). `executors_explicit(config)` tells them apart. `asdict()`/`deepcopy` keep the subclass, so DEFAULTS stays derived.
- File-scoped rules (global `executors`, `claude_quota`+`executors` in one file, repo `executor` ≠ first) live in `config._apply_executors()`, not `_validate()` (which only sees merged values).
- `scheduler_quota.decide()` walks `fallback_order()` with `blocked()`; credits question via `orch_inbox.ask_once(key="credits:<name>")` only when nothing could start. Spend: `StateStore.record_spend/spend_today` (UTC day, append-only `spend.jsonl`); runner records via `run_execution._record_spend` → `scheduler_quota.record_cost`.
- Locked test needs `codex`+`api` accepted although the order said claude only; decide() holds unmeasured api executors (`MEASURED`). Rationale in that run's REBUTTAL.
- `tests/test_config.py` exact-defaults tests compare `asdict(config)`; new Config fields must be added there.
- tests/acceptance/test_settings_* fail on base (no `fa settings` command yet) — not a regression signal.
