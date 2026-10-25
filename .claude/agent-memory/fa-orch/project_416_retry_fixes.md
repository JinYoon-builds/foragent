---
name: project-416-retry-fixes
description: #416 retry keeps work — retry marker file, previous-attempt summary, review class instructions, majority-only conflict rebuild and the locked tests it contradicts
metadata:
  type: project
---

- Retry default: `orch._retry` writes `<state>/hints/<n>.retry`; the hint file keeps only the given hint (test_orch_verbs reads hints/1.txt == hint exactly). `run_previous.pending_hint()` returns (retry_default, hint); prompts render a separate `Retry instructions:` section before `Orchestrator hint:` (unit tests assert `"Orchestrator hint:\n" + hint` and `startswith("Orchestrator hint:")`). Both files are consumed only on a normal executor result.
- Stop point lives at `<state>/runs/issue-N-stop.txt`, NOT inside `runs/issue-N/` (test_run_prompts asserts that dir is absent after a clean merge). Summary is added to the fresh prompt only when failure/reviewer text exists (test_executor_choice forbids "Previous attempt" in the first prompt).
- Reviewer class question lives in `review_scope_rules()`, so codex prompt, CLAUDE.local.md scope and PR body all get it; `tests/runner_support.expected_review_rules` mirrors it.
- Conflict rule: `ConflictBudgetExhausted` carries conflicts + own paths (merge-base..HEAD, MERGE_HEAD when merging). `_rebuild_after_conflicts` rebuilds only if >half, else flags retries-exceeded naming files (messages/conflicts.py). Patch replay now resolves in place first via `_resolve_conflicts(own_paths=, failure=)`; in-place success returns "" and the publish path sets `resume_verified`.
- **Why:** issue decisions. Locked `test_resume_short_lock::test_exhausted_resume_conflict_budget_still_rebuilds...` (1/2 conflict) contradicts #416's exactly-half test → REBUTTAL. test_risk_map freezes "at most 5 findings" (already failing on base).
- **How to apply:** if a later issue touches rebuilds, re-check those two locked files first.
- Review round 2: a replay failure with no conflict paths must stop (0 own files conflict), not recreate. `_prepare_target` resets the local branch to the destination before applying, so a stop first restores the saved `head_sha()` + `staged_tree_sha()` (`read-tree --reset -u`); restoring at the merge base would drop branch history and make the WIP push non-fast-forward. Human text added to PR bodies (review class question) goes through `messages/conflicts.py` with `config.language`; agent prompts stay English.
- Review round 3: stop point is recorded in a `finally` around `_available_executor` (limit waits, launch errors, SystemExit end the turn without a result → log output since the turn's offset). Replay `own_paths` = merge-base..HEAD via `_own_paths(attempt, start_ref)` computed before the worktree is reset; uncommitted staged work counts as zero own files. A replay conflict stop also restores the source head+tree.
