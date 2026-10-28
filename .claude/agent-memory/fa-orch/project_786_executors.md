---
name: project-786-executors
description: #786 ordered executors list — executor_list field + executors property, decide() fallback order, spend.jsonl, credits question key
metadata:
  type: project
---
- `Config.executor_list` holds only the repo-written list (None = unset); `Config.executors` is a property returning it or the legacy list derived from executor/claude_quota, so `replace()`/`lane_config()` re-derive per lane. `executors_explicit(config)` = executor_list is not None.
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
- Review 4: one reviewer helper `choose_reviewer()`; only claude<->codex fallbacks switch reviewer, other fallbacks keep configured. Merge with #769 roles: keep both validators in config_validation.
- Review 5: `_lane_held(ctx, lane, now, number)` at all 3 callers (start loop, _machine_budget, facts _admission); written null keep_percent/spend_cap is a ConfigError.
- Review 6: every fallback branch of choose_reviewer must check availability (non claude<->codex: configured if free, else own, else none).
- Retry 8: baseline regressions came from the derived list leaking into YAML dumps of asdict(config) (fixed for good in round 9) and module line limit (scheduler_quota split into quota_reviewer.py/quota_spend.py). Always run full acceptance vs origin/main (git archive origin/main src into .foragent/base, PYTHONPATH) before finishing.
- Round 9 redesign: Config stores only the written list in `executor_list` (None = unset); `Config.executors` is a property (explicit or derived). `asdict()` then writes `executor_list: null`, so round-trips need no bypass and executors+claude_quota in one file is always a ConfigError. `FILE_KEYS={"executors"}` must be allowed wherever file keys are checked (config._read_yaml, run_base_move). DerivedExecutors and the YAML representer are gone — supersedes the earlier lines above.
- Acceptance compare: when origin/main is ahead of merge-base, a "new" failure may be a feature merged later on main; confirm by running that file against `git archive $(git merge-base HEAD origin/main)`.
- Round 10: spend comes from the adapter (`ExecResult.cost_usd` set in ClaudeExecutor.run from the stream result event or the review JSON document, which has no `type`), not from re-parsing log lines.
