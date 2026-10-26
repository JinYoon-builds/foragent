---
name: project-318-usage-recheck
description: #318 early quota reset — usage_recheck.py, note_limit baseline, lane-pause clearing, conftest stub for real codex app-server
metadata:
  type: project
---

- `usage_recheck.recheck(ctx)` runs in `scheduler.tick()` after reaping and *before* the GitHub-wait early return (free reads must continue during GitHub throttling). It reads `executor-pauses.json`, and per paused executor re-reads every `usage_recheck_minutes` (config, default 10) — first read happens on the first tick that sees the pause (no pause-start time needed; the acceptance interval test still passes).
- Baseline: `run_execution` calls `usage_recheck.note_limit()` at the limit, saving exhausted windows and their percent in `<state>/usage-recheck.json`. Recovery = every fresh window < 100 and each exhausted window below its level at the limit (Claude `rejected` at 60% needs < 60). Unknown baseline → only "< 100" required.
- Codex window names come from `windowDurationMins` via `usage_events._window_name` (300→"5h", 10080→"7d"), matching log-parsed names. Claude snapshot freshness uses `seen_at` (≤ 2×interval, not before the limit), never mtime.
- Clearing also deletes lane pauses for lanes whose implementer or reviewer is that executor and whose until ≤ the executor's until.
- `tests/conftest.py::no_real_usage_source` stubs `read_codex`/`read_claude` unless the test requests the `codex_server` fixture, so ticks with paused executors never spawn the real `codex app-server`.

**Why:** spec docs/specs/2026-10-24-early-reset.md; acceptance tests/acceptance/test_usage_recheck.py.
**How to apply:** #186/#238 (headroom choice) can reuse `read_codex`/`read_claude`.
- Review fixes: a Claude rejection with no named window (`limit`, level None) recovers when every fresh window < 100. Clearing is compare-and-clear under the state lock: the mark (record with checked_at) is written *before* the read, and the pause string plus mark must be unchanged, so a renewal during the read survives.
- Review 2: `note_limit()` runs before `pause_executor()`, so it takes max(active pause, until) as its deadline and merges the active note's exhausted windows (lowest level per name). Otherwise an earlier-reset repeat limit mismatches the pause deadline and recheck falls back to an empty baseline.
- Review 3 (defect class: lost concurrent writes): `note_limit()` now writes the executor pause and its note in one state-lock section (`pause_executor()` afterwards is a no-op). `_claim()` writes the recheck mark as compare-and-set against the pause string read at tick start, skipping a note whose until is ahead of the pause. flock is per open file, so never nest `store._state_lock()` or call a locking StateStore method inside it; write the JSON directly.
- Merge 2026-10-26: the crashed WIP commit carried old main files (overlap_files, run_overlap, git.py = #527) and committed nested conflict markers in config.py/test_config.py. Take `git show MERGE_HEAD:path`, reapply #318's few lines, then check that `git diff --stat MERGE_HEAD` lists only #318 files. Validation keys now live in `config_validation.py`.
