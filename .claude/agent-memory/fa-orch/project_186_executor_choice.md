---
name: project-186-executor-choice
description: #186 headroom executor choice — where the logic lives, line limits hit, unfinished regressions
metadata:
  type: project
---
- Headroom math and order live in `quota_headroom.py`; start journal (switch + same-model-review) and `choose_executor` in `quota_events.py`, re-exported from `scheduler_quota` and the `scheduler` facade (scheduler.py is at its 250-line cap; scheduler_*.py and state_runs.py cap at 300).
- Run stores `executor`/`reviewer` (None reviewer = no review); `implementer` stays only for old records. Runner uses `quota_reviewer.stored_config`; `RunContext.lane_executor` keeps the lane's configured executor for the lane-pause check (`run_execution.lane_default`).
- `Config.order_written` is derived in `_apply_executors`; a written-back `order_written` key wins because asdict always writes `claude_quota`.
- `hard_limited` = blocked key `switch_reason_limit` only (spend caps still hold a start).
- Records with only `implementer` use `quota_reviewer.legacy_config` (pre-#186 rule: only a Claude pause moves the review) — test_run_execution's paused-reviewer test needs it.
- Rule 3 reads usage.json, so `usage_recheck.recheck` must `record_usage` the fresh windows when it clears a pause; otherwise the stale 100% window holds the recovered executor (test_usage_recheck[codex]).
- Base failures at the time (not #186): test_settings_cmd 3, test_settings_window 4, test_runner_split line limits (capacity/run_review/run_publish).
- Review r2: a kept executor is released on any hard limit (blocked key `switch_reason_limit`: pause or used-up window), not only a used-up window; capped subscriptions run on credits and are not released. A stored reviewer at a hard limit moves at review launch (`quota_reviewer.review_moved`, journals same-model-review/limit), not in `stored_config`. `order_written` is per file (written-back bool speaks only for its file), and a repository list always wins. The usage recheck writes its reading inside `_clear`'s lock via `StateStore._write_usage`, and only when no newer `seen_at` exists.
